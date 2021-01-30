# This file is part of Scapy
# See http://www.secdev.org/projects/scapy for more information
# Copyright (C) Nils Weiss <nils@we155.de>
# This program is published under a GPLv2 license

# scapy.contrib.description = Python-Can CANSocket
# scapy.contrib.status = loads

"""
Python-CAN CANSocket Wrapper.
"""

import time
import struct
import threading
import copy

from functools import reduce
from operator import add

from scapy.config import conf
from scapy.supersocket import SuperSocket
from scapy.layers.can import CAN
from scapy.error import warning
from scapy.modules.six.moves import queue
from can import Message as can_Message
from can import CanError as can_CanError
from can import BusABC as can_BusABC
from can.interface import Bus as can_Bus


class SocketMapper:
    def __init__(self, bus, sockets):
        self.bus = bus           # type: can_BusABC
        self.sockets = sockets   # type: list[SocketWrapper]

    def mux(self):
        while True:
            try:
                msg = self.bus.recv(timeout=0)
                if msg is None:
                    return
                for sock in self.sockets:
                    if sock._matches_filters(msg):
                        sock.rx_queue.put(copy.copy(msg))
            except Exception as e:
                warning("[MUX] python-can exception caught: %s" % e)


class SocketsPool(object):
    __instance = None

    def __new__(cls):
        if SocketsPool.__instance is None:
            SocketsPool.__instance = object.__new__(cls)
            SocketsPool.__instance.pool = dict()
            SocketsPool.__instance.pool_mutex = threading.Lock()
        return SocketsPool.__instance

    def internal_send(self, sender, msg):
        with self.pool_mutex:
            try:
                mapper = self.pool[sender.name]
                mapper.bus.send(msg)
                for sock in mapper.sockets:
                    if sock == sender:
                        continue
                    if not sock._matches_filters(msg):
                        continue

                    m = copy.copy(msg)
                    m.timestamp = time.time()
                    sock.rx_queue.put(m)
            except KeyError:
                warning("[SND] Socket %s not found in pool" % sender.name)
            except can_CanError as e:
                warning("[SND] python-can exception caught: %s" % e)

    def multiplex_rx_packets(self):
        with self.pool_mutex:
            for _, t in self.pool.items():
                t.mux()

    def register(self, socket, *args, **kwargs):
        k = str(
            str(kwargs.get("bustype", "unknown_bustype")) + "_" +
            str(kwargs.get("channel", "unknown_channel"))
        )
        with self.pool_mutex:
            if k in self.pool:
                t = self.pool[k]
                t.sockets.append(socket)
                filters = [s.filters for s in t.sockets
                           if s.filters is not None]
                if filters:
                    t.bus.set_filters(reduce(add, filters))
                socket.name = k
            else:
                bus = can_Bus(*args, **kwargs)
                socket.name = k
                self.pool[k] = SocketMapper(bus, [socket])

    def unregister(self, socket):
        with self.pool_mutex:
            try:
                t = self.pool[socket.name]
                t.sockets.remove(socket)
                if not t.sockets:
                    t.bus.shutdown()
                    del self.pool[socket.name]
            except KeyError:
                warning("Socket %s already removed from pool" % socket.name)


class SocketWrapper(can_BusABC):
    """Socket for specific Bus or Interface.
    """

    def __init__(self, *args, **kwargs):
        super(SocketWrapper, self).__init__(*args, **kwargs)
        self.rx_queue = queue.Queue()  # type: queue.Queue[can_Message]
        self.name = None
        SocketsPool().register(self, *args, **kwargs)

    def _recv_internal(self, timeout):
        SocketsPool().multiplex_rx_packets()
        try:
            return self.rx_queue.get(block=True, timeout=timeout), True
        except queue.Empty:
            return None, True

    def send(self, msg, timeout=None):
        SocketsPool().internal_send(self, msg)

    def shutdown(self):
        SocketsPool().unregister(self)


class PythonCANSocket(SuperSocket):
    desc = "read/write packets at a given CAN interface " \
           "using a python-can bus object"
    nonblocking_socket = True

    def __init__(self, **kwargs):
        self.basecls = kwargs.pop("basecls", CAN)
        self.iface = SocketWrapper(**kwargs)

    def recv_raw(self, x=0xffff):
        msg = self.iface.recv()

        hdr = msg.is_extended_id << 31 | msg.is_remote_frame << 30 | \
            msg.is_error_frame << 29 | msg.arbitration_id

        if conf.contribs['CAN']['swap-bytes']:
            hdr = struct.unpack("<I", struct.pack(">I", hdr))[0]

        dlc = msg.dlc << 24
        pkt_data = struct.pack("!II", hdr, dlc) + bytes(msg.data)
        return self.basecls, pkt_data, msg.timestamp

    def send(self, x):
        msg = can_Message(is_remote_frame=x.flags == 0x2,
                          is_extended_id=x.flags == 0x4,
                          is_error_frame=x.flags == 0x1,
                          arbitration_id=x.identifier,
                          dlc=x.length,
                          data=bytes(x)[8:])
        try:
            x.sent_time = time.time()
        except AttributeError:
            pass
        self.iface.send(msg)

    @staticmethod
    def select(sockets, *args, **kwargs):
        SocketsPool().multiplex_rx_packets()
        return [s for s in sockets if isinstance(s, PythonCANSocket) and
                not s.iface.rx_queue.empty()], PythonCANSocket.recv

    def close(self):
        if self.closed:
            return
        super(PythonCANSocket, self).close()
        self.iface.shutdown()


CANSocket = PythonCANSocket
