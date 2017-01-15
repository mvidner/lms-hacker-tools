#!/usr/bin/env python

# The MIT License (MIT)

# Copyright (c) 2015 David Lechner <david@lechnology.com>

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import print_function
import argparse
import os
import sys
import yaml
import codecs
import struct
import re
from cStringIO import StringIO
from ctypes import *

from lms2012 import *

def parse_program_header(infile, size):
    header = ProgramHeader()
    infile.readinto(header)
    if header.lego != 'LEGO':
        raise ValueError("Bad file - does not start with 'LEGO'")
    if header.size != size:
        raise ValueError("Bad file - size is incorrect")
    return header.byte_code_version, header.num_objects, header.global_bytes

def parse_object_header(infile):
    header = ObjectHeader()
    infile.readinto(header)
    return header

def parse_object(infile, outfile, id):
    header = parse_object_header(infile)
    save_position = infile.tell()
    infile.seek(header.offset)
    num_args = 0
    arg_bytes = 0
    if header.is_vmthread:
        type = "vmthread"
    elif header.is_subcall:
        type = "subcall"
        num_args = ord(infile.read(1))
    elif header.is_block:
        type = "block"
    else:
        raise ValueError("Unknown object type")
    print("{0} OBJECT{1}".format(type, id), file=outfile)
    print("{", file=outfile)
    if num_args:
        for i in range(num_args):
            type = Callparam(ord(infile.read(1)))
            format = type.data_format
            string_size = 0
            if format is DataFormat.DATAS:
                string_size = ord(infile.read(1))
            string_size_str = ''
            if string_size:
                string_size_str = " {0}".format(string_size)
            print("\t", type.name, " LOCAL", arg_bytes, string_size_str, sep='', file=outfile)
            arg_bytes += string_size or format.size

        print(file=outfile)
    if header.local_bytes - arg_bytes:
        for i in range(arg_bytes, header.local_bytes):
            print("\tDATA8 LOCAL", i, sep='', file=outfile)
        print(file=outfile)
    while True:
        offset = infile.tell()
        line = parse_ops(infile, header.offset, id)
        if not line:
            break
        print("OFFSET", id, "_", offset - header.offset, ":", sep='', file=outfile)
        if line == "RETURN()":
            # skip printing "RETURN()" if it is the last op in an object
            peek = ord(infile.read(1))
            infile.seek(-1, os.SEEK_CUR)
            if peek == Op.OBJECT_END.value:
                continue
        print("\t", line, sep='', file=outfile)
    print("}", file=outfile)
    infile.seek(save_position)

def parse_sysops(infile):
    global filename

    byte = infile.read(1)
    if len(byte) == 0:
        return None
    op = SysOp(ord(byte))
    params = []
    for param in op.params:
        params.append(parse_primpar(param, infile))

    # HACK: dump the uploaded file
    if op == SysOp.BEGIN_DOWNLOAD:
        path = params[1][1:-1]  # strip quotes
        _dir, filename = os.path.split(path)
        try:
            os.remove(filename)
        except OSError:
            pass
    elif op == SysOp.CONTINUE_DOWNLOAD:
        data = infile.read()
        print("DUMPING {0} bytes to {1}".format(len(data), filename))
        with open(filename, "ab") as f:
            f.write(bytearray(data))

    return "{0}({1})".format(op.name, ",".join(params))

def parse_ops(infile, start, id):
    byte = infile.read(1)
    if len(byte) == 0:
        return None
    op = Op(ord(byte))
    if op == Op.OBJECT_END:
        return None
    params = []
    for param in op.params:
        if isinstance(param, Subparam):
            value = parse_param(param, infile)
            params.append(parse_subparam(param, value, infile))
        else:
            params.append(parse_param(param, infile))
        # special handling for CALL
        if op.name == "CALL" and param is Param.PAR16:
            params[-1] = "OBJECT{0}".format(params[-1])
        # special handling for varargs
        if param is Param.PARNO:
            value = int(params[-1])
            del params[-1]
            for i in range(value):
                params.append(parse_param(Param.PARV, infile))
    # special handling for jump ops
    if op.name[:2] == "JR":
        offset32 = int(params[-1])
        offset = offset32 % 65536
        if offset >= 32768:
            offset -= 65536
        del params[-1]
        params.append("OFFSET{0}_{1}".format(id, infile.tell() - start + offset))
    return "{0}({1})".format(op.name, ",".join(params))

def parse_primpar(sizecode, infile):
    data = None
    if sizecode == PRIMPAR_1_BYTE:
        data = struct.unpack('B', infile.read(1))[0]
    elif sizecode == PRIMPAR_2_BYTES:
        data = struct.unpack('<H', infile.read(2))[0]
    elif sizecode == PRIMPAR_4_BYTES:
        data = struct.unpack('<L', infile.read(4))[0]
    elif sizecode == PRIMPAR_STRING or sizecode == PRIMPAR_STRING_OLD:
        return quote_string(parse_string(infile))
    else:
        raise ValueError("unexpected primpar {0}".format(sizecode))
    return str(data)

def parse_param(param, infile):
    first_byte = ord(infile.read(1))
    if first_byte & PRIMPAR_LONG:
        if first_byte & PRIMPAR_VARIABLE:
            if first_byte & PRIMPAR_GLOBAL:
                scope = 'GLOBAL'
            else:
                scope = 'LOCAL'
            size = first_byte & PRIMPAR_BYTES
            data = parse_primpar(size, infile)
            handle = ''
            if first_byte & PRIMPAR_HANDLE:
                handle = '@'
            elif first_byte & PRIMPAR_ADDR:
                raise NotImplementedError()
            return "{0}{1}{2}".format(handle,scope,data)
        else: # PRIMPAR_CONST
            if first_byte & PRIMPAR_LABEL:
                return "LABEL{0}".format(ord(infile.read(1)))
            size = first_byte & PRIMPAR_BYTES
            if param is Param.PARF:
                if size != PRIMPAR_4_BYTES:
                    raise ValueError("Expecting float value")
                bytes = infile.read(4)
                data = struct.unpack('f', bytes)[0]
                int_value = struct.unpack('<L', bytes)[0]
                if int_value == DATAF_MAX:
                    return "DATAF_MAX"
                if int_value == DATAF_MIN:
                    return "DATAF_MIN"
                if int_value == DATAF_NAN:
                    return "DATAF_NAN"
                return str(data) + "F"
            return parse_primpar(size, infile)
    else:  # PRIMPAR_SHORT
        if first_byte & PRIMPAR_VARIABLE:
            if first_byte & PRIMPAR_GLOBAL:
                scope = 'GLOBAL'
            else:
                scope = 'LOCAL'
            return "{0}{1}".format(scope, first_byte & PRIMPAR_INDEX)
        else:
            if first_byte & PRIMPAR_CONST_SIGN:
                # special handling for negative numbers
                return str((first_byte & PRIMPAR_VALUE) - (PRIMPAR_VALUE + 1))
            return str(first_byte & PRIMPAR_VALUE)

    raise NotImpementedError("TODO")

def parse_subparam(type, value, infile):
#    print("PARSE_SUBPARAM {0} {1}".format(repr(type), repr(value)))

    try:
        subcode_type = type.subcode_type(int(value))
    except:
        return "DAMMIT"
    params = [ subcode_type.name ]
    values = -1
    for param in subcode_type.params:
        # special handling for arrays
        if param is Param.PARVALUES:
            values = int(params[-1])
            # print("PARVALUES = ", values)
        elif values >= 0:
            while values > 0:
                values -= 1
                params.append(parse_param(param, infile))
        else:
            params.append(parse_param(param, infile))
            # special handling for varargs
            if param is Param.PARNO:
                if not int(params[-1]):
                    del params[-1]
                else:
                    for i in range(int(params[-1])):
                        params.append(parse_param(Param.PARV, infile))
    return ",".join(params)

def parse_string(infile):
    value = ''
    while True:
        ch = infile.read(1)
        if not ord(ch):
            break
        value += ch
    return value

def quote_string(value):
    value = value.replace("\t", "\\t")
    value = value.replace("\r", "\\r")
    value = value.replace("\n", "\\n")
    value = value.replace("'", "\\q")
    return "'{0}'".format(value)

def u16(infile):
    return ord(infile.read(1)) + 256 * ord(infile.read(1))

def u8(infile):
    return ord(infile.read(1))

def parse_sent(infile, actual_size):
    size = u16(infile)
    if size != actual_size - 2:
        print("\t", "# INCOMPLETE packet, ignoring. wanted {0} actual {1}".format(size, actual_size - 2))
        return False
    msgid = u16(infile)
    type = u8(infile)
    print("\t", "# MSG #{0}".format(msgid))

    local_global = None
    if type == 0x00 or type == 0x80: # direct command
        local_global = u16(infile)
        nlocal = local_global >> 10
        nglobal = local_global & 0x3ff
        print("\t", "# LOCAL: {0}, GLOBAL: {1}".format(nlocal, nglobal))

        while True:
            line = parse_ops(infile, 0, 42)
            if not line:
                break
            print("\t", line)
    elif type == 0x01 or type == 0x81: # system command
        print("\t", "# SYSOP")
        # one only, to allow for arbitrary payload
        line = parse_sysops(infile)
        print("\t", line)
#        while True:
#            line = parse_sysops(infile)
#            if not line:
#                break
#            print("\t", line)
    else:
        print("\t", "# #{0}".format(type))

    return True

def parse_received(infile):
    size = u16(infile)
    msgid = u16(infile)
    type = u8(infile)
    print("\t", "# FOR #{0}".format(msgid))
    if type == DIRECT_REPLY:
        print("\t", "# OK")
        parse_generic_reply(infile)
    elif type == SYSTEM_REPLY:
        print("\t", "# SYSTEM OK")
        parse_system_reply(infile)
    elif type == DIRECT_REPLY_ERROR:
        print("\t", "# ERROR")
        parse_generic_reply(infile)
    elif type == SYSTEM_REPLY_ERROR:
        print("\t", "# SYSTEM ERROR")
        parse_system_reply(infile)
    else:
        print("\t", "# unknown type")

def parse_generic_reply(infile):
    bytes = infile.read(4)
    data = struct.unpack('f', bytes)[0]
    print("\t", "# FLOAT? {0}".format(data))

def parse_system_reply(infile):
    to_command = SysOp(u8(infile))
    status = SysOpReturn(u8(infile))
    print("\t", "# cmd was {0}, status {1}".format(to_command, status))

def parse_communication(infile):
    decode_hex = codecs.getdecoder("hex_codec")
    serial_comm = yaml.load(infile)

    buffer = ""                 # sent data that was not complete the last time
    for record in serial_comm:
        hexdata = record["hexdata"]
        print("DATA ", " ".join(re.findall("..", hexdata)))
        data = decode_hex(hexdata)[0]
        dfile = StringIO(buffer + data)
        if record["sent"]:
            done = parse_sent(dfile, len(buffer + data))
            if done:
                buffer = ""
            else:
                buffer = data
        else:
            parse_received(dfile)

def main():
    parser = argparse.ArgumentParser(description='Disassemble lms2012 byte codes.')
    parser.add_argument('input', type=argparse.FileType('rb', 0),
                       help='The .rbf file to disassemble.')
    parser.add_argument('-o', '--output', type=argparse.FileType('wb', 0), default='-',
                       help='The .lms file that will contain the result.')
    parser.add_argument('-e', '--escape', action='store_true',
                       help='Do it better')
    args = parser.parse_args()

    if args.escape:
        parse_communication(args.input)
        sys.exit(0)

    file_size = os.path.getsize(args.input.name)
    version, num_objs, global_bytes = parse_program_header(args.input, file_size)
    print("// Disassembly of", args.input.name, file=args.output)
    print("//", file=args.output)
    print("// Byte code version:", version, file=args.output)
    print(file=args.output)
    for i in range(global_bytes):
        print("DATA8 GLOBAL", i, sep='', file=args.output)
    for i in range(num_objs):
        print(file=args.output)
        parse_object(args.input, args.output, i+1)

if __name__ == '__main__':
    main()
