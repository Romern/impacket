# Impacket - Collection of Python classes for working with network protocols.
#
# Copyright Fortra, LLC and its affiliated companies
# All rights reserved.
#
# This software is provided under a slightly modified version of the Apache
# Software License. See the accompanying LICENSE file for more information.

"""Offline MS-RPRN BIDI wire-format regressions, independent of a print server."""

import struct
from unittest.mock import Mock

import pytest

from impacket.dcerpc.v5 import rprn
from impacket.dcerpc.v5.dtypes import NULL
from impacket.dcerpc.v5.rpcrt import DCERPC


class Wire:
    """Build reference stubs without using Impacket's NDR serializers."""

    def __init__(self, ndr64):
        self.ndr64 = ndr64
        self.data = bytearray()
        self.padding = []

    def align(self, alignment):
        size = -len(self.data) % alignment
        self.padding.extend(range(len(self.data), len(self.data) + size))
        self.data.extend(b'\x00' * size)

    def dword(self, value):
        self.align(4)
        self.data.extend(struct.pack('<L', value))

    def count(self, value):
        self.align(8 if self.ndr64 else 4)
        self.data.extend(struct.pack('<Q' if self.ndr64 else '<L', value))

    def string(self, value):
        encoded = value.encode('utf-16le')
        self.count(len(encoded) // 2)
        self.count(0)
        self.count(len(encoded) // 2)
        self.data.extend(encoded)

    def assert_matches(self, actual):
        actual = bytearray(actual)
        assert len(actual) == len(self.data)
        # NDR padding bytes have unspecified values.
        for offset in self.padding:
            actual[offset] = 0
        assert actual == self.data


# Signed integers, both scalar union arms, all string arms (including NULL),
# and counted binary data, including empty and NULL buffers.
VALUES = [
    (rprn.BIDI_NULL, 0),
    (rprn.BIDI_INT, -123456),
    (rprn.BIDI_FLOAT, 1.25),
    (rprn.BIDI_BOOL, 1),
    (rprn.BIDI_STRING, 'Printer\x00'),
    (rprn.BIDI_TEXT, 'Gr\u00fc\u00dfe\x00'),
    (rprn.BIDI_ENUM, 'Ready\x00'),
    (rprn.BIDI_STRING, None),
    (rprn.BIDI_BLOB, b'\x00\xff\x80\x01\x02'),
    (rprn.BIDI_BLOB, b''),
    (rprn.BIDI_BLOB, None),
]


def container_wire(wire, values, response=False, null_schemas=False):
    wire.count(len(values))  # Conformance precedes the three DWORDs.
    wire.dword(1)
    wire.dword(0)
    wire.dword(len(values))
    wire.align(8 if wire.ndr64 else 4)
    deferred = []
    for index, (kind, value) in enumerate(values):
        schema = '\\Printer.Test%d\x00' % index
        wire.align(8 if wire.ndr64 else 4)
        if response:
            wire.dword(0 if index % 2 == 0 else 13005)
        wire.dword(100 + index)
        wire.count(0 if null_schemas else 0x20004 + index * 8)  # pSchema
        wire.align(8 if wire.ndr64 else 4)
        wire.dword(kind)  # dwBidiType
        wire.align(8 if wire.ndr64 else 4)
        wire.dword(kind)  # Non-encapsulated union discriminator
        wire.align(8 if wire.ndr64 else 4)
        if kind in (rprn.BIDI_NULL, rprn.BIDI_BOOL, rprn.BIDI_INT):
            wire.data.extend(struct.pack('<l', value))
        elif kind == rprn.BIDI_FLOAT:
            wire.data.extend(struct.pack('<f', value))
        else:
            if kind == rprn.BIDI_BLOB:
                wire.dword(0 if value is None else len(value))
            wire.count(0 if value is None else 0x20008 + index * 8)
        wire.align(8 if wire.ndr64 else 4)
        deferred.append((schema, kind, value))
    for schema, kind, value in deferred:
        if not null_schemas:
            wire.string(schema)
        if value is not None:
            if kind in (rprn.BIDI_STRING, rprn.BIDI_TEXT, rprn.BIDI_ENUM):
                wire.string(value)
            elif kind == rprn.BIDI_BLOB:
                wire.count(len(value))
                wire.data.extend(value)


def make_container(values, response=False, ndr64=False, null_schemas=False):
    cls = rprn.RPC_BIDI_RESPONSE_CONTAINER if response else rprn.RPC_BIDI_REQUEST_CONTAINER
    item_cls = rprn.RPC_BIDI_RESPONSE_DATA if response else rprn.RPC_BIDI_REQUEST_DATA
    container = cls(isNDR64=ndr64)
    container['Version'] = 1
    container['Flags'] = 0
    container['Count'] = len(values)
    items = []
    for index, (kind, value) in enumerate(values):
        item = item_cls(isNDR64=ndr64)
        if response:
            item['dwResult'] = 0 if index % 2 == 0 else 13005
        item['dwReqNumber'] = 100 + index
        item['pSchema'] = NULL if null_schemas else '\\Printer.Test%d\x00' % index
        if not null_schemas:
            item.fields['pSchema']['ReferentID'] = 0x20004 + index * 8
        item['data']['dwBidiType'] = kind
        union = item['data']['u']
        union['tag'] = kind
        if kind in (rprn.BIDI_NULL, rprn.BIDI_BOOL):
            union['bData'] = value
        elif kind == rprn.BIDI_INT:
            union['iData'] = value
        elif kind == rprn.BIDI_FLOAT:
            union['fData'] = value
        elif kind == rprn.BIDI_BLOB:
            union['biData']['cbBuf'] = 0 if value is None else len(value)
            union['biData']['pszString'] = NULL if value is None else value
            if value is not None:
                union['biData'].fields['pszString']['ReferentID'] = 0x20008 + index * 8
        else:
            union['sData'] = NULL if value is None else value
            if value is not None:
                union.fields['sData']['ReferentID'] = 0x20008 + index * 8
        items.append(item)
    container['aData'] = items
    return container


def check_items(container, values, response=False, null_schemas=False):
    assert container['Version'] == 1
    assert container['Flags'] == 0
    assert container['Count'] == len(values)
    assert len(container['aData']) == len(values)
    for index, (item, (kind, value)) in enumerate(zip(container['aData'], values)):
        assert item['dwReqNumber'] == 100 + index
        if null_schemas:
            assert item.fields['pSchema']['ReferentID'] == 0
        else:
            assert item['pSchema'] == '\\Printer.Test%d\x00' % index
        if response:
            assert item['dwResult'] == (0 if index % 2 == 0 else 13005)
        assert item['data']['dwBidiType'] == kind
        union = item['data']['u']
        assert union['tag'] == kind
        if kind in (rprn.BIDI_NULL, rprn.BIDI_BOOL):
            assert union['bData'] == value
        elif kind == rprn.BIDI_INT:
            assert union['iData'] == value
        elif kind == rprn.BIDI_FLOAT:
            assert union['fData'] == value
        elif kind == rprn.BIDI_BLOB:
            assert union['biData']['cbBuf'] == (0 if value is None else len(value))
            if value is None:
                assert union['biData'].fields['pszString']['ReferentID'] == 0
            else:
                assert b''.join(union['biData']['pszString']) == value
        else:
            if value is None:
                assert union.fields['sData']['ReferentID'] == 0
            else:
                assert union['sData'] == value


@pytest.mark.parametrize('ndr64', [False, True])
@pytest.mark.parametrize('action', ['Get', 'EnumSchema', 'GetAll'])
@pytest.mark.parametrize('values', [[], *[[value] for value in VALUES], VALUES])
def test_request_wire(ndr64, values, action):
    wire = Wire(ndr64)
    wire.data.extend(b'\x11' * 20)
    wire.count(0x20000)
    wire.string(action + '\x00')
    container_wire(wire, values)
    request = rprn.RpcSendRecvBidiData(isNDR64=ndr64)
    request['hPrinter'] = b'\x11' * 20
    request['pAction'] = action + '\x00'
    request.fields['pAction']['ReferentID'] = 0x20000
    request['pReqData'] = make_container(values, ndr64=ndr64)
    wire.assert_matches(request.getData())
    parsed = rprn.RpcSendRecvBidiData(bytes(wire.data), isNDR64=ndr64)
    assert parsed['hPrinter'] == b'\x11' * 20
    assert parsed['pAction'] == action + '\x00'
    check_items(parsed['pReqData'], values)


@pytest.mark.parametrize('ndr64', [False, True])
@pytest.mark.parametrize('null_schemas', [False, True])
@pytest.mark.parametrize('values', [[], *[[value] for value in VALUES], VALUES])
def test_response_wire(ndr64, values, null_schemas):
    wire = Wire(ndr64)
    wire.count(0x20000)
    container_wire(wire, values, response=True, null_schemas=null_schemas)
    wire.dword(0)
    response = rprn.RpcSendRecvBidiDataResponse(isNDR64=ndr64)
    response['ppRespData'] = make_container(values, response=True, ndr64=ndr64, null_schemas=null_schemas)
    response.fields['ppRespData']['ReferentID'] = 0x20000
    response['ErrorCode'] = 0
    wire.assert_matches(response.getData())
    parsed = rprn.RpcSendRecvBidiDataResponse(bytes(wire.data), isNDR64=ndr64)
    assert parsed['ErrorCode'] == 0
    check_items(parsed['ppRespData'], values, response=True, null_schemas=null_schemas)


@pytest.mark.parametrize('ndr64', [False, True])
@pytest.mark.parametrize('status', [0, 50, 87])
def test_null_response(ndr64, status):
    raw = struct.pack('<QL' if ndr64 else '<LL', 0, status)
    response = rprn.RpcSendRecvBidiDataResponse(raw, isNDR64=ndr64)
    assert response.fields['ppRespData']['ReferentID'] == 0
    assert response['ErrorCode'] == status
    assert response.getData() == raw


@pytest.mark.parametrize('action', ['EnumSchema', 'EnumSchema\x00', NULL])
def test_helper(action):
    dce = Mock()
    container = make_container([])
    result = rprn.hRpcSendRecvBidiData(dce, b'\x11' * 20, action, container)
    request = dce.request.call_args[0][0]
    assert isinstance(request, rprn.RpcSendRecvBidiData)
    assert request['pAction'] == (0 if action is NULL else 'EnumSchema\x00')
    assert request.fields['pReqData'] is container
    assert result is dce.request.return_value
    assert request.opnum == 97
    assert rprn.OPNUMS[97] == (rprn.RpcSendRecvBidiData, rprn.RpcSendRecvBidiDataResponse)


@pytest.mark.parametrize('ndr64', [False, True])
def test_helper_method_error(ndr64):
    dce = Mock()
    dce.NDR64Syntax = DCERPC.NDR64Syntax
    dce.transfer_syntax = DCERPC.NDR64Syntax if ndr64 else DCERPC.NDRSyntax
    dce.request = lambda request: DCERPC.request(dce, request)
    dce.recv.return_value = struct.pack('<QL' if ndr64 else '<LL', 0, 50)
    with pytest.raises(rprn.DCERPCSessionError) as exc:
        rprn.hRpcSendRecvBidiData(dce, b'\x11' * 20, 'EnumSchema', make_container([]))
    assert exc.value.get_error_code() == 50
    assert exc.value.get_packet().fields['ppRespData']['ReferentID'] == 0


@pytest.mark.parametrize('ndr64', [False, True])
def test_helper_transfer_syntax(ndr64):
    # Real DCERPC.request converts objects initially created in NDR32 to the
    # negotiated syntax, recursively including all array elements and unions.
    expected_request = Wire(ndr64)
    expected_request.data.extend(b'\x11' * 20)
    expected_request.count(0x20000)
    expected_request.string('Get\x00')
    container_wire(expected_request, VALUES)
    expected_response = Wire(ndr64)
    expected_response.count(0x20000)
    container_wire(expected_response, VALUES, response=True)
    expected_response.dword(0)

    def call(opnum, request, uuid):
        assert opnum == 97
        request.fields['pAction']['ReferentID'] = 0x20000
        expected_request.assert_matches(request.getData())

    dce = Mock()
    dce.NDR64Syntax = DCERPC.NDR64Syntax
    dce.transfer_syntax = DCERPC.NDR64Syntax if ndr64 else DCERPC.NDRSyntax
    dce.call = call
    dce.recv.return_value = bytes(expected_response.data)
    dce.request = lambda request: DCERPC.request(dce, request)
    response = rprn.hRpcSendRecvBidiData(dce, b'\x11' * 20, 'Get', make_container(VALUES))
    assert response['ErrorCode'] == 0
    check_items(response['ppRespData'], VALUES, response=True)
