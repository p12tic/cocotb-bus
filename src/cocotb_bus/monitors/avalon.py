# Copyright cocotb contributors
# Copyright (c) 2013 Potential Ventures Ltd
# Copyright (c) 2013 SolarFlare Communications Inc
# Licensed under the Revised BSD License, see LICENSE for details.
# SPDX-License-Identifier: BSD-3-Clause

"""Monitors for Intel Avalon interfaces.

See https://www.intel.com/content/dam/www/programmable/us/en/pdfs/literature/manual/mnl_avalon_spec_1_3.pdf

NB Currently we only support a very small subset of functionality.
"""

import warnings

from scapy.utils import hexdump

from cocotb.triggers import RisingEdge
from cocotb.binary import BinaryValue

from cocotb_bus.monitors import BusMonitor


class AvalonProtocolError(Exception):
    pass


class AvalonST(BusMonitor):
    """Avalon-ST bus.

    Non-packetized so each valid word is a separate transaction.
    """

    _signals = ["valid", "data"]
    _optional_signals = ["ready"]

    _default_config = {"firstSymbolInHighOrderBits": True}

    def __init__(self, entity, name, clock, *, config={}, **kwargs):
        BusMonitor.__init__(self, entity, name, clock, **kwargs)

        self.config = self._default_config.copy()

        for configoption, value in config.items():
            self.config[configoption] = value
            self.log.debug("Setting config option %s to %s", configoption, str(value))

    async def _monitor_recv(self):
        """Watch the pins and reconstruct transactions."""

        # Avoid spurious object creation by recycling
        clkedge = RisingEdge(self.clock)

        def valid():
            if hasattr(self.bus, "ready"):
                return self.bus.valid.value and self.bus.ready.value
            return self.bus.valid.value

        # NB could await on valid here more efficiently?
        while True:
            await clkedge
            if valid():
                vec = self.bus.data.value
                vec.big_endian = self.config["firstSymbolInHighOrderBits"]
                self._recv(vec.buff)


class AvalonSTPkts(BusMonitor):
    """Packetized Avalon-ST bus.

    Args:
        entity, name, clock: see :class:`BusMonitor`
        config (dict): bus configuration options
        report_channel (bool): report channel with data, default is False
            Setting to True on bus without channel signal will give an error
    """

    _signals = ["valid", "data", "startofpacket", "endofpacket"]
    _optional_signals = ["error", "channel", "ready", "empty"]

    _default_config = {
        "dataBitsPerSymbol"             : 8,
        "firstSymbolInHighOrderBits"    : True,
        "maxChannel"                    : 0,
        "readyLatency"                  : 0,
        "invalidTimeout"                : 0,
    }

    def __init__(self, entity, name, clock, *, config={}, report_channel=False, **kwargs):
        BusMonitor.__init__(self, entity, name , clock, **kwargs)

        self.config = self._default_config.copy()
        self.report_channel = report_channel

        # Set default config maxChannel to max value on channel bus
        if hasattr(self.bus, 'channel'):
            self.config['maxChannel'] = (2 ** len(self.bus.channel)) -1
        else:
            if report_channel:
                raise ValueError("Channel reporting asked on bus without channel signal")

        for configoption, value in config.items():
            self.config[configoption] = value
            self.log.debug("Setting config option %s to %s",
                           configoption, str(value))

        num_data_symbols = (len(self.bus.data) /
                            self.config["dataBitsPerSymbol"])
        if (num_data_symbols > 1 and not hasattr(self.bus, 'empty')):
            raise AttributeError(
                "%s has %i data symbols, but contains no object named empty" %
                (self.name, num_data_symbols))

        self.config["useEmpty"] = (num_data_symbols > 1)

        if hasattr(self.bus, 'channel'):
            if len(self.bus.channel) > 128:
                raise AttributeError("AvalonST interface specification defines channel width as 1-128. "
                                     "%d channel width is %d" %
                                     (self.name, len(self.bus.channel)))
            maxChannel = (2 ** len(self.bus.channel)) -1
            if self.config['maxChannel'] > maxChannel:
                raise AttributeError("%s has maxChannel=%d, but can only support a maximum channel of "
                                     "(2**channel_width)-1=%d, channel_width=%d" %
                                     (self.name, self.config['maxChannel'], maxChannel, len(self.bus.channel)))

    async def _monitor_recv(self):
        """Watch the pins and reconstruct transactions."""

        # Avoid spurious object creation by recycling
        clkedge = RisingEdge(self.clock)
        pkt = b""
        in_pkt = False
        invalid_cyclecount = 0
        channel = None

        def valid():
            if hasattr(self.bus, 'ready'):
                return self.bus.valid.value and self.bus.ready.value
            return self.bus.valid.value

        while True:
            await clkedge

            if self.in_reset:
                continue

            if valid():
                invalid_cyclecount = 0

                if self.bus.startofpacket.value:
                    if pkt:
                        raise AvalonProtocolError("Duplicate start-of-packet received on %s" %
                                                  str(self.bus.startofpacket))
                    pkt = b""
                    in_pkt = True

                if not in_pkt:
                    raise AvalonProtocolError("Data transfer outside of "
                                              "packet")

                # Handle empty and X's in empty / data
                vec = BinaryValue()
                if not self.bus.endofpacket.value:
                    vec = self.bus.data.value
                else:
                    value = self.bus.data.value.get_binstr()
                    if self.config["useEmpty"] and self.bus.empty.value.integer:
                        empty = self.bus.empty.value.integer * self.config["dataBitsPerSymbol"]
                        if self.config["firstSymbolInHighOrderBits"]:
                            value = value[:-empty]
                        else:
                            value = value[empty:]
                    vec.assign(value)
                    if not vec.is_resolvable:
                        raise AvalonProtocolError("After empty masking value is still bad?  "
                                                  "Had empty {:d}, got value {:s}".format(empty,
                                                                                          self.bus.data.value.get_binstr()))

                vec.big_endian = self.config['firstSymbolInHighOrderBits']
                pkt += vec.buff

                if hasattr(self.bus, 'channel'):
                    if channel is None:
                        channel = self.bus.channel.value.integer
                        if channel > self.config["maxChannel"]:
                            raise AvalonProtocolError("Channel value (%d) is greater than maxChannel (%d)" %
                                                      (channel, self.config["maxChannel"]))
                    elif self.bus.channel.value.integer != channel:
                        raise AvalonProtocolError("Channel value changed during packet")

                if self.bus.endofpacket.value:
                    self.log.info("Received a packet of %d bytes", len(pkt))
                    self.log.debug(f"Received Packet:\n{hexdump(pkt, dump=True)}")
                    self.channel = channel
                    if self.report_channel:
                        self._recv({"data": pkt, "channel": channel})
                    else:
                        self._recv(pkt)
                    pkt = b""
                    in_pkt = False
                    channel = None
            else:
                if in_pkt:
                    invalid_cyclecount += 1
                    if self.config["invalidTimeout"]:
                        if invalid_cyclecount >= self.config["invalidTimeout"]:
                            raise AvalonProtocolError(
                                "In-Packet Timeout. Didn't receive any valid data for %d cycles!" %
                                invalid_cyclecount)


class AvalonSTPktsWithChannel(AvalonSTPkts):
    """Packetized AvalonST bus using channel.

    This class is deprecated. Use AvalonSTPkts(..., report_channel=True, ...)
    """

    def __init__(self, entity, name, clock, **kwargs):
        warnings.warn(
            "Use of AvalonSTPktsWithChannel is deprecated\n"
            "\tUse AvalonSTPkts(..., report_channel=True, ...)",
            DeprecationWarning, stacklevel=2
        )
        AvalonSTPkts.__init__(self, entity, name, clock, report_channel=True, **kwargs)
