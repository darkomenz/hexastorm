"""
    spi_statemachine.py
    Example for laser scanner
    Has a lot of the complexity present in laser scanner but works with a simple LED.

    Rik Starmans
"""
from collections import namedtuple

import unittest
from migen.fhdl import verilog
from migen.fhdl.tools import list_special_ios
from migen import *
from litex.soc.cores.spi import SPIMaster, SPISlave

import sys
sys.path.append("..") 
import hexa as board

#TODO: not all these variables needed for simple example
#      reduce variable to LED blinking speed and move this to other file
# VARIABLES = {'RPM':2400,'SPINUP_TICKS':1.5,'MAX_WAIT_STABLE_TICKS':1.125, 'FACETS':4,
#             'SCANLINE_DATA_SIZE':790,'TICKS_PER_PRISM_FACET':12500, 'TICKS_START':4375,
#              'SINGLE_FACET':0, 'DIRECTION':0, 'JITTER_ALLOW':300, 'JITTER_THRESH':400}
# # lines that can be in memory
# LINES = (LEDPROGRAM.MEMWIDTH*LEDPROGRAM.MEMDEPTH)//VARIABLES['SCANLINE_DATA_SIZE']


class LEDProgram(Module):
    @staticmethod
    def commands():
        commands = ('RECVCOMMAND', 'STATUS', 'START', 'STOP', 'READ_D', 'WRITE_L')
        Commands = namedtuple('Commands', commands, defaults=tuple(range(len(commands))))
        return Commands()

    COMMANDS = commands.__func__()
    CHUNKSIZE = 8 # you write in chunks of 8 bytes
    MEMWIDTH = 8  # must be 8, as you receive in terms of eight
    

    def __init__(self, spi_port, led, memdepth=512, maxperiod=5):
        self.MEMDEPTH = memdepth
        self.MAXPERIOD = maxperiod
        # three submodules; SPI receiver, memory and laser state machine
        # full byte state
        self.ledstate   =  Signal(3)   # state laser module 6-8 byte
        self.error =  Signal(4)  # error state  1-5 byte, 
                            #     -- bit 0 read error
                            # memory full  0 byte
        debug = Signal(8)   # optional 

        # Memory element
        # Current idea: memory continiously repeats cycle;  idle --> read --> write
        # Rules:
        #        read cannot be set equal to write address  --> reading a line not written yet
        #        write cannot be set equal to read address  --> writing on a line which is still in use
        # Nextwrite and nextread addres used to know when writing is finished
        # writebyte, current byte written to
        # readbit, current bit read
        # readaddress, current address read
        # written to detect if already information is written to memory
        # sram memory is 32 blocks... each block has its own ports
        # one block 8*512 = 4096 bits currently used
        self.specials.mem = Memory(width=self.MEMWIDTH, depth=self.MEMDEPTH)
        writeport = self.mem.get_port(write_capable=True, mode = READ_FIRST)
        readport = self.mem.get_port(has_re=True)
        self.specials += writeport, readport
        self.ios = {writeport.adr, writeport.dat_w, writeport.we, readport.dat_r, readport.adr, readport.re}
        self.submodules.memory = FSM(reset_state = "RESET")
        readbit = Signal(max = self.MEMWIDTH)
        self.writebyte = Signal(max=self.MEMDEPTH)
        written = Signal()
        dat_r_temp = Signal(max= self.MEMWIDTH)
        self.memory.act("RESET",
                NextValue(written, 0),
                NextValue(readport.re, 1),
                NextValue(readport.adr, 0),
                NextValue(writeport.adr, 0),
                NextState("IDLE")
        )
        # this state is useless
        self.memory.act("IDLE",
            NextState("IDLE")
        )
        # Receiver State Machine
        # Consists out of component from litex and own custom component
        # Detects whether new command is available
        spislave = SPISlave(spi_port, data_width=8)
        self.submodules.slave = spislave
        # COMMANDS 
        # The command variable contains command to be executed
        # typically the recvcommand, cannot be set externally
        command = Signal(max=len(self.COMMANDS))
        # Done detector
        done_d = Signal()
        done_rise = Signal()
        self.sync += done_d.eq(spislave.done)
        self.comb += done_rise.eq(spislave.done & ~done_d)
        # Start detector (could be refactored)
        start_d = Signal()
        start_rise = Signal()
        self.sync += start_d.eq(spislave.start)
        self.comb += start_rise.eq(spislave.start & ~start_d)
        # Custom Receiver
        self.submodules.receiver = FSM(reset_state = "IDLE")
        self.receiver.act("IDLE",
        #NOTE: simplify with cat
                NextValue(spislave.miso[1:5], self.error),
                NextValue(spislave.miso[5:], self.ledstate),
            If((writeport.adr==readport.adr)&(written==1),
                NextValue(spislave.miso[0],1)
            ).
            Else(
                NextValue(spislave.miso[0],0)
            ),
            If(start_rise,
                NextState("WAITFORDONE")
            )
        )
        self.receiver.act("WAITFORDONE",
            If(done_rise,
                NextState("PROCESSINPUT")
            )
        )
        self.receiver.act("PROCESSINPUT",
            NextState("IDLE"),
            # Read Header
            If(command == self.COMMANDS.RECVCOMMAND,
                If(spislave.mosi == self.COMMANDS.STOP,
                    NextValue(self.ledstate, 0)
                ).
                Elif(spislave.mosi == self.COMMANDS.START,
                    NextValue(self.ledstate, 1)
                ).
                Elif(spislave.mosi == self.COMMANDS.READ_D,
                    NextValue(command, self.COMMANDS.READ_D),
                    #NOTE doesn't work as you jump to idle where miso is changed
                    NextValue(spislave.miso, debug)
                ).
                Elif(spislave.mosi == self.COMMANDS.WRITE_L,
                    # only switch to write stage if memory is not full
                    If((writeport.adr==readport.adr)&(written==1),
                        NextValue(command, self.COMMANDS.RECVCOMMAND)
                    ).
                    Else(
                        NextValue(command, self.COMMANDS.WRITE_L)
                    )
                )
                # Else; Command invalid or memory full nothing happens
            ).
            # Read data after header; only applicable for debug or write line
            Else(
                If(command == self.COMMANDS.READ_D,
                    NextValue(command, self.COMMANDS.RECVCOMMAND),
                ).
                # command must be WRITE_L
                Else(
                    NextValue(written, 1),
                    NextValue(writeport.dat_w, spislave.mosi),
                    NextValue(writeport.we, 1),
                    NextState("WRITE"),
                    If(self.writebyte>=self.CHUNKSIZE-1,
                        NextValue(self.writebyte, 0),
                        NextValue(command, self.COMMANDS.RECVCOMMAND)
                    ).
                    Else(NextValue(self.writebyte, self.writebyte+1)
                    )
                )
            )
        )
        self.receiver.act("WRITE",
            NextValue(writeport.adr, writeport.adr+1), 
            NextValue(writeport.we, 0),
            NextState("IDLE")
        )
        # LED State machine
        # A led blinks every so many cycles.
        # The blink rate of the LED can be limited via a counter
        counter = Signal(16)
        #counter = Signal(max=self.MAXPERIOD.bit_length())
        self.submodules.ledfsm = FSM(reset_state = "OFF")
        self.ledfsm.act("OFF",
            NextValue(led, 0),
            NextValue(self.error[0], 0), # there is no read error 
            NextValue(readbit,0),
            If(self.ledstate==1,
                 NextState("ON")
            )
        )
        read = Signal()  # to indicate wether you have read
        self.ledfsm.act("ON",
            If(counter == maxperiod-1,
               NextValue(counter, 0),
               # if there is no data, led off and report error
               #NOTE: would also make sense to report error if not been written yet and you try to read
               If(written==0,
                    NextValue(read, 0), # you nead to read again, wrong value
                    NextValue(led, 0),
                    NextValue(self.error[0], 1)
               ).
               Else(
                    NextValue(self.error[0], 0),
                    NextValue(dat_r_temp, dat_r_temp>>1),
                    NextValue(led, dat_r_temp[0]),
                    NextValue(readbit, readbit+1),
                    # you need to read again!
                    # move to next addres if end is reached
                    If(readbit==self.MEMWIDTH-1,
                        NextValue(read, 0),
                        NextValue(readport.adr, readport.adr+1),
                        If(readport.adr+1==writeport.adr,
                            NextValue(written,0)
                        ).
                        #NOTE: count wrap around
                        Elif((readport.adr+1==self.MEMDEPTH)&(writeport.adr==0),
                            NextValue(written,0)
                        )
                )
               )
            ).
            Else(
                NextValue(counter, counter+1)
            ),
            If(self.ledstate==0,
               NextState("OFF")
            ),
            #TODO: can't you make this combinatorial
            If(read==0,
               NextState("READ"),
               NextValue(readport.re, 0)
            )
        )
        
        self.ledfsm.act("READ",
            #NOTE: counter should be larger than 3
            NextValue(counter, counter+1),
            NextValue(readport.re, 1),
            NextValue(dat_r_temp, readport.dat_r),
            NextValue(read, 1),
            NextState("ON")
        )



class TestSPIStateMachine(unittest.TestCase):
    def setUp(self):
        class DUT(Module):
            def __init__(self):
                pads = Record([("clk", 1), ("cs_n", 1), ("mosi", 1), ("miso", 1)])
                self.submodules.master = SPIMaster(pads, data_width=8,
                        sys_clk_freq=100e6, spi_clk_freq=5e6,
                        with_csr=False)
                self.led = Signal()
                self.submodules.ledprogram = LEDProgram(pads, self.led, memdepth=16)
        self.dut = DUT()

    def transaction(self, data_sent, data_received):
        ''' 
        helper function to test transaction from raspberry pi side
        '''
        yield self.dut.master.mosi.eq(data_sent)
        yield self.dut.master.length.eq(8)
        yield self.dut.master.start.eq(1)
        yield
        yield self.dut.master.start.eq(0)
        yield
        while (yield self.dut.master.done) == 0:
            yield
        self.assertEqual((yield self.dut.master.miso), data_received)

    def test_ledstatechange(self):
        def raspberry_side():
            # get the initial status
            yield from self.transaction(LEDProgram.COMMANDS.STATUS, 0)
            # turn on the LED, status should still be zero
            yield from self.transaction(LEDProgram.COMMANDS.START, 0)
            # check wether the led is on
            # error is reported as nothing has been written yetS
            yield from self.transaction(LEDProgram.COMMANDS.STATUS, int('100010',2))
            # turn OFF the led state machine
            yield from self.transaction(LEDProgram.COMMANDS.STOP, int('100010',2))
            # LED state machine should be off and the led off
            yield from self.transaction(LEDProgram.COMMANDS.STATUS, 0)

        def fpga_side():
            timeout = 0
            # LED statemachine should be off on the start
            self.assertEqual((yield self.dut.ledprogram.ledstate), 0)
            # wait till led state changes
            while (yield self.dut.ledprogram.ledstate) == 0:
                timeout += 1
                if timeout>1000:
                    raise Exception("Led doesn't turn on.")
                yield
            timeout = 0
            # LED statemachine should be one now
            # Wether LED is on depends on data
            self.assertEqual((yield self.dut.ledprogram.ledstate), 1)
            # LED should be off
            self.assertEqual((yield self.dut.led), 0)
            # wait till led state changes
            while (yield self.dut.ledprogram.ledstate) == 1:
                timeout += 1
                if timeout>1000:
                    raise Exception("Led doesn't turn off.")
                yield
            # LED statemachine should be off
            self.assertEqual((yield self.dut.ledprogram.ledstate), 0)
        run_simulation(self.dut, [raspberry_side(), fpga_side()])


    def test_writedata(self):
        def raspberry_side():
            # write lines to memory
            for i in range(self.dut.ledprogram.MEMDEPTH+1):
                data_byte = i%256 # bytes can't be larger than 255
                if i%(LEDProgram.CHUNKSIZE)==0:
                    if (i>0)&((i%self.dut.ledprogram.MEMDEPTH)==0):
                        # check if memory is full
                        yield from self.transaction(LEDProgram.COMMANDS.WRITE_L, 1)
                        continue
                    else:
                        yield from self.transaction(LEDProgram.COMMANDS.WRITE_L, 0)
                yield from self.transaction(data_byte, 0)
            # memory is tested in litex
            in_memory = []
            loops = 10
            for i in range(loops):
                value = (yield self.dut.ledprogram.mem[i])
                in_memory.append(value)
            self.assertEqual(list(range(loops)),in_memory)
        run_simulation(self.dut, [raspberry_side()])


    def test_ledstatechangepostwrite(self):
        def raspberry_side():
            # get the initial status
            yield from self.transaction(LEDProgram.COMMANDS.STATUS, 0)
            # write lines to memory
            for i in range(self.dut.ledprogram.MEMDEPTH+1):
                data_byte = i%256 # bytes can't be larger than 255
                if i%(LEDProgram.CHUNKSIZE)==0:
                    if (i>0)&((i%self.dut.ledprogram.MEMDEPTH)==0):
                        # check if memory is full
                        yield from self.transaction(LEDProgram.COMMANDS.WRITE_L, 1)
                        continue
                    else:
                        yield from self.transaction(LEDProgram.COMMANDS.WRITE_L, 0)
                yield from self.transaction(data_byte, 0)
            # turn on the LED, status should be one as memory is full
            yield from self.transaction(LEDProgram.COMMANDS.START, 1)
            # ledstate should change
            timeout = 0
            while (yield self.dut.ledprogram.ledstate) == 0:
                timeout += 1
                if timeout>1000:
                    raise Exception("Led state doesnt go on.")
                yield
            # status should be memory full and led on
            yield from self.transaction(LEDProgram.COMMANDS.STATUS, int('100001',2))
            # led should turn on now
            timeout = 0
            while (yield self.dut.led) == 0:
                timeout += 1
                if timeout>1000:
                    raise Exception("Led doesn't turn on.")
                yield
            # you know LED is on now
            # LED should be on for three ticks
            count = 0
            while (yield self.dut.led) == 1:
                count += 1
                if count>1000:
                    raise Exception("Led doesn't turn on.")
                yield
            self.assertEqual(count, self.dut.ledprogram.MAXPERIOD)
            # you could count until led is zero and then 1 again as check
            # check if you receive read errorS
            while (yield self.dut.ledprogram.error) == 0:
                timeout += 1
                if timeout>1000:
                    raise Exception("Don't receive read error.")
                yield
            # status should be memory empty, led statemachine on and read error
            # you do get an error so written must zero
            yield from self.transaction(LEDProgram.COMMANDS.STATUS, int('100010',2))
        run_simulation(self.dut, [raspberry_side()])




    # what tests do you need?
    #   -- memory empty, can't read  --> led doesn't turn on
    #   -- memory full, can read --> up to some point




if __name__ == '__main__':
    import sys
    if len(sys.argv)>1:
        if sys.argv[1] == 'build':
            plat = board.Platform()
            spi_port = plat.request("spi")
            led = plat.request("user_led")
            spi_statemachine = LEDProgram(spi_port, led)
            plat.build(spi_statemachine, build_name = 'spi_statemachine')
    else:
        unittest.main()