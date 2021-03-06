"""
pghoard - test wal utility functions

Copyright (c) 2015 Ohmu Ltd
See LICENSE for details
"""
import codecs
import pytest
import struct
from pghoard import wal

WAL_HEADER_95 = codecs.decode(b"87d006002f0000000000009c1100000000000000", "hex")


def wal_header_for_file(name):
    tli, log, seg = wal.name_to_tli_log_seg(name)
    pageaddr = (log << 32) | (seg * wal.XLOG_SEG_SIZE)
    return struct.pack("=HHIQI", list(wal.WAL_MAGIC).pop(0), 0, tli, pageaddr, 0)


def test_wal_header():
    blob95 = WAL_HEADER_95
    hdr95 = wal.WalHeader(version=90500, timeline=47, lsn='11/9C000000', filename='0000002F000000110000009C')
    assert wal.read_header(blob95) == hdr95
    # only first 20 bytes are used
    assert wal.read_header(blob95 + b"XXX") == hdr95
    with pytest.raises(ValueError):
        wal.read_header(blob95[:18])
    blob94 = b"\x7e\xd0" + blob95[2:]
    hdr94 = hdr95._replace(version=90400)
    assert wal.read_header(blob94) == hdr94
    blob9X = b"\x7F\xd0" + blob95[2:]
    with pytest.raises(KeyError):
        wal.read_header(blob9X)


def test_lsn_from_name():
    assert wal.lsn_from_name("0000002E0000001100000004") == "11/4000000"
    assert wal.lsn_from_name("000000FF0000001100000004") == "11/4000000"


def test_construct_wal_name():
    sysinfo = {
        "dbname": "",
        "systemid": "6181331723016416192",
        "timeline": "4",
        "xlogpos": "F/190001B0",
    }
    assert wal.construct_wal_name(sysinfo) == wal.name_for_tli_log_seg(4, 0xF, 0x19)
    assert wal.construct_wal_name(sysinfo) == "000000040000000F00000019"
