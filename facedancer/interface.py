#
# This file is part of facedancer.
#
""" Functionality for defining USB interfaces. """

# Support annotations on Python < 3.9
from __future__  import annotations

import struct
import textwrap

from typing       import Dict, List, Iterable
from dataclasses  import field
from collections  import defaultdict

from .magic       import instantiate_subordinates, AutoInstantiable
from .types       import USBDirection, USBStandardRequests

from .            import device
from .descriptor  import USBDescribable, USBDescriptor, USBClassDescriptor, USBDescriptorTypeNumber, StringRef
from .request     import USBControlRequest, USBRequestHandler, get_request_handler_methods
from .request     import standard_request_handler, to_this_interface
from .endpoint    import USBEndpoint

from .logging     import log


class USBInterface(USBDescribable, AutoInstantiable, USBRequestHandler):
    """ Class representing a USBDevice interface.

    Fields:
        number :
            The interface's index. Zero indexed.
        class_number, subclass_number, protocol_number :
            The USB class adhered to on this interface; usually a USBDeviceClass constant.
        interface_string :
            A short, descriptive string used to identify the endpoint; or None if not provided.
    """
    DESCRIPTOR_TYPE_NUMBER = 0x4

    name                   : StringRef = StringRef.field(string="generic USB interface")

    number                 : int = 0
    alternate              : int = 0

    class_number           : int = 0
    subclass_number        : int = 0
    protocol_number        : int = 0

    interface_string       : str = None

    # Descriptors that will be included in a GET_CONFIGURATION response.
    attached_descriptors     : List[USBDescriptor] = field(default_factory=list)

    # Descriptors that can be requested with the GET_DESCRIPTOR request.
    requestable_descriptors    : Dict[tuple[int, int], USBDescriptor] = field(default_factory=dict)

    endpoints              : Dict[int, USBEndpoint] = field(default_factory=dict)
    parent                 : USBDescribable = None


    @classmethod
    def from_binary_descriptor(cls, data, strings={}):
        """
        Generates an interface object from a descriptor.
        """
        interface_number, alternate_setting, num_endpoints, interface_class, \
                interface_subclass, interface_protocol, string_index \
                = struct.unpack_from("xxBBBBBBB", data)
        return cls(
            name=None,
            number=interface_number,
            alternate=alternate_setting,
            class_number=interface_class,
            subclass_number=interface_subclass,
            protocol_number=interface_protocol,
            interface_string=StringRef.lookup(strings, string_index)
        )


    def __post_init__(self):

        self.interface_string = StringRef.ensure(self.interface_string)

        # Capture any descriptors/endpoints declared directly on the class.
        for endpoint in instantiate_subordinates(self, USBEndpoint):
            self.add_endpoint(endpoint)
        for descriptor in instantiate_subordinates(self, USBDescriptor):
            self.add_descriptor(descriptor)

        # Populate our request handlers.
        self._request_handler_methods = get_request_handler_methods(self)


    #
    # User interface.
    #

    def get_device(self):
        """ Returns the device associated with the given descriptor. """
        return self.parent.get_device()


    def add_endpoint(self, endpoint: USBEndpoint):
        """ Adds the provided endpoint to the interface. """
        if endpoint.address in self.endpoints:
            ep_name = type(endpoint).__name__
            ep_addr = f"0x{endpoint.address:02X}"
            other = self.endpoints[endpoint.address]
            other_name = type(other).__name__
            raise Exception(
                f"Endpoint of type {ep_name} cannot be added to this "
                f"interface because there is already an endpoint of "
                f"type {other_name} with the same address {ep_addr}")
        else:
            self.endpoints[endpoint.address] = endpoint
            endpoint.parent = self


    def get_endpoint(self, endpoint_number: int, direction: USBDirection) -> USBEndpoint:
        """ Attempts to find a subordinate endpoint matching the given number/direction.

        Args:
            endpoint_number : The endpoint number to search for.
            direction       : The endpoint direction to be matched.

        Returns:
            The matching endpoint; or None if no matching endpoint existed.
        """
        address = USBEndpoint.address_for_number(endpoint_number, direction)
        return self.endpoints.get(address, None)


    def has_endpoint(self, endpoint_number: int, direction: USBDirection) -> USBEndpoint:
        """ Returns true iff we have matching subordinate endpoint.

        Args:
            endpoint_number : The endpoint number to search for.
            direction       : The endpoint direction to be matched.
        """
        return (self.get_endpoint(endpoint_number, direction) is not None)


    def add_descriptor(self, descriptor: USBDescriptor):
        """ Adds the provided descriptor to the interface. """
        identifier = descriptor.get_identifier()
        desc_name = type(descriptor).__name__

        if descriptor.include_in_config:
            self.attached_descriptors.append(descriptor)
            descriptor.parent = self

        elif descriptor.number is None:
            raise Exception(
                f"Descriptor of type {desc_name} cannot be added to this "
                f"interface because it is not to be included in the "
                f"configuration descriptor, yet does not have a number "
                f"to request it separately with")

        elif identifier in self.requestable_descriptors:
            other = self.requestable_descriptors[identifier]
            other_name = type(other).__name__
            other_type = f"0x{other.type_number:02X}"
            raise Exception(
                f"Descriptor of type {desc_name} cannot be added to this "
                f"interface because there is already a descriptor of type "
                f"{other_name} with the same type code {other_type} and "
                f"number {other.number}")

        else:
            self.requestable_descriptors[identifier] = descriptor
            descriptor.parent = self


    #
    # Event handlers.
    #

    def handle_data_received(self, endpoint: USBEndpoint, data: bytes):
        """ Handler for receipt of non-control request data.

        Typically, this method will delegate any data received to the
        appropriate configuration/interface/endpoint. If overridden, the
        overriding function will receive all data; and can delegate it by
        calling the `.handle_data_received` method on `self.configuration`.

        Args:
            endpoint_number : The endpoint number on which the data was received.
            data            : The raw bytes received on the relevant endpoint.
        """

        if self.has_endpoint(endpoint.number, endpoint.direction):
            endpoint.handle_data_received(data)
        else:
            self.get_device().handle_unexpected_data_received(endpoint.number, data)


    def handle_data_requested(self, endpoint: USBEndpoint):
        """ Handler called when the host requests data on a non-control endpoint.

        Typically, this method will delegate the request to the appropriate
        interface+endpoint. If overridden, the overriding function will receive
        all data.

        Args:
            endpoint_number : The endpoint number on which the host requested data.
        """

        if self.has_endpoint(endpoint.number, endpoint.direction):
            endpoint.handle_data_requested()
        else:
            self.get_device().handle_unexpected_data_requested(endpoint.number)


    def handle_buffer_empty(self, endpoint: USBEndpoint):
        """ Handler called when a given endpoint first has an empty buffer.

        Often, an empty buffer indicates an opportunity to queue data
        for sending ('prime an endpoint'), but doesn't necessarily mean
        that the host is planning on reading the data.

        This function is called only once per buffer.
        """

        if self.has_endpoint(endpoint.number, endpoint.direction):
            endpoint.handle_buffer_empty()


    #
    # Backend helpers.
    #

    def get_endpoints(self):
        """ Returns an iterable over all endpoints in this interface. """
        return self.endpoints.values()


    #
    # Internal interface.
    #

    @standard_request_handler(number=USBStandardRequests.GET_DESCRIPTOR)
    @to_this_interface
    def handle_get_descriptor_request(self, request):
        """ Handle GET_DESCRIPTOR requests; per USB2 [9.4.3] """
        log.debug("Handling GET_DESCRIPTOR on endpoint.")

        # This is the same as the USBDevice get descriptor request => avoid duplication.
        self.get_device().handle_generic_get_descriptor_request(self, request)


    # Table 9-12 of USB 2.0 spec (pdf page 296)
    def get_descriptor(self) -> bytes:
        """ Retrieves the given interface's interface descriptor, with subordinates. """

        # FIXME: use construct

        string_manager = self.get_device().strings

        d = bytearray([
                9,          # length of descriptor in bytes
                4,          # descriptor type 4 == interface
                self.number,
                self.alternate,
                len(self.endpoints),
                self.class_number,
                self.subclass_number,
                self.protocol_number,
                string_manager.get_index(self.interface_string)
        ])

        for descriptor in self.attached_descriptors:
            if callable(descriptor):
                d += descriptor()
            else:
                d += descriptor

        # ... append each endpoint's endpoint descriptor.
        for e in self.endpoints.values():
            d += e.get_descriptor()
            for descriptor in e.attached_descriptors:
                if callable(descriptor):
                    d += descriptor()
                else:
                    d += descriptor


        return d

    #
    # Alternate interface support.
    #

    @standard_request_handler(number=USBStandardRequests.SET_INTERFACE)
    @to_this_interface
    def handle_set_interface_request(self, request: USBControlRequest):
        """ Handle SET_INTERFACE requests; per USB2 [9.4.10] """
        log.debug(f"f{self.name} received SET_INTERFACE request")

        configuration = self.parent
        device = configuration.parent
        backend = device.backend

        if device.configuration is None:
            request.stall()
        else:
            try:
                # Find this alternate setting and switch to it.
                number = request.index_low
                alternate = request.value
                identifier = (number, alternate)
                interface = configuration.interfaces[identifier]
                configuration.active_interfaces[number] = interface
                # Reset the data toggles of this interface's endpoints.
                for endpoint in interface.endpoints.values():
                    backend.clear_halt(endpoint.number, endpoint.direction)
                request.acknowledge()
            except KeyError:
                request.stall()

    @standard_request_handler(number=USBStandardRequests.GET_INTERFACE)
    @to_this_interface
    def handle_get_interface_request(self, request):
        """ Handle GET_INTERFACE requests; per USB2 [9.4.4] """
        log.debug("received GET_INTERFACE request")

        configuration = self.parent
        device = configuration.parent

        if device.configuration is None:
            request.stall()
        else:
            try:
                number = request.index_low
                interface = configuration.active_interfaces[number]
                request.reply(bytes([interface.alternate]))
            except KeyError:
                request.stall()


    #
    # Automatic instantiation support.
    #

    def get_identifier(self) -> (int, int):
        return (self.number, self.alternate)


    # Although we identify interfaces by (number, alternate), this helper
    # is called from the request handling code, where we only want to
    # match by interface number. The correct alternate interface should have
    # been selected earlier in the request handling process.
    def matches_identifier(self, other: int) -> bool:
        return (other == self.number)


    #
    # Request handler functions.
    #

    def _request_handlers(self) -> Iterable[callable]:
        return self._request_handler_methods

    def _get_subordinate_handlers(self) -> Iterable[callable]:
        return self.endpoints.values()


    def generate_code(self, name=None, indent=0):

        if name is None:
            if self.alternate == 0:
                name = f"Interface_{self.number}"
            else:
                name = f"Interface_{self.number}_{self.alternate}"

        code = f"""
class {name}(USBInterface):
    number           = {self.number}
    alternate        = {self.alternate}
    class_number     = {self.class_number}
    subclass_number  = {self.subclass_number}
    protocol_number  = {self.protocol_number}
    interface_string = {self.interface_string.generate_code()}
"""

        # Use alphabetic suffixes to distinguish between multiple attached
        # descriptors with the same type number.
        suffixes = defaultdict(lambda: 'A')

        for descriptor in self.attached_descriptors:
            type_number = descriptor.type_number
            suffix = suffixes[type_number]
            suffixes[type_number] = chr(ord(suffix) + 1)
            name = f"Descriptor_0x{type_number:02X}_{suffix}"
            code += descriptor.generate_code(name=name, indent=4)

        for endpoint in self.endpoints.values():
            code += endpoint.generate_code(indent=4)

        for descriptor_id in sorted(self.requestable_descriptors):
            descriptor = self.requestable_descriptors[descriptor_id]
            code += descriptor.generate_code(indent=4)

        return textwrap.indent(code, indent * ' ')
