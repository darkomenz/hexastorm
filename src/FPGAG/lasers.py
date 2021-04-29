import unittest
from struct import unpack
from copy import deepcopy
from random import randint

from nmigen import Signal, Elaboratable
from nmigen import Module
from luna.gateware.test import LunaGatewareTestCase, sync_test_case
from luna.gateware.memory import TransactionalizedFIFO

import FPGAG.controller as controller
from FPGAG.constants import (MEMWIDTH, WORD_BYTES, INSTRUCTIONS)
from FPGAG.platforms import TestPlatform


def params(platform):
    '''determines parameters for laser scanner

    returns dictionary
    '''
    var = platform.laser_var
    var['POLY_HZ'] = var['RPM']/60
    if platform.name == 'Test':
        var['CRYSTAL_HZ'] = round(var['TICKSINFACET']*var['FACETS']
                                  * var['POLY_HZ'])
        var['LASER_HZ'] = var['CRYSTAL_HZ']/var['LASERTICKS']
        var['SPINUP_TIME'] = 10/var['CRYSTAL_HZ']
        # TODO: stop scanline seems to affect the stable thresh?!
        # can be 30 without stopline (this is from old repo)
        var['STABLE_TIME'] = 5*var['TICKSINFACET']/var['CRYSTAL_HZ']
        var['START%'] = 2/var['TICKSINFACET']
        var['END%'] = ((var['LASERTICKS']*var['SCANBITS'])/var['TICKSINFACET']
                       + var['START%'])
        assert var['TICKSINFACET'] == round(var['CRYSTAL_HZ']/(var['POLY_HZ']
                                        * var['FACETS']))
    else:
        var['TICKSINFACET'] = round(var['CRYSTAL_HZ']/(var['POLY_HZ']
                                        * var['FACETS']))
    # parameter creation
    var['LASERTICKS'] = int(var['CRYSTAL_HZ']/var['LASER_HZ'])
    # jitter requires 2
    # you also need to enable read pin at count one when you read bits
    assert var['LASERTICKS']>2
    var['JITTERTICKS'] = round(0.5*var['LASERTICKS'])
    if var['END%'] > round(1-(var['JITTERTICKS']+1)
                           / var['TICKSINFACET']):
        raise Exception("Invalid settings, END% too high")
    var['BITSINSCANLINE'] = round((var['TICKSINFACET'] *
                                  (var['END%']-var['START%']))
                                  / var['LASERTICKS'])
    if platform.name == 'Test':
        assert var['BITSINSCANLINE'] == var['SCANBITS']
    if var['BITSINSCANLINE'] <= 0:
        raise Exception("Bits in scanline invalid")
    var['SPINUPTICKS'] = round(var['SPINUP_TIME']*var['CRYSTAL_HZ'])
    var['STABLETICKS'] = round(var['STABLE_TIME']*var['CRYSTAL_HZ'])
    var['POLYPERIOD'] = int(var['CRYSTAL_HZ']/(var['POLY_HZ']*6*2))
    return var


class Laserhead(Elaboratable):
    """ Controller of laser scanner with rotating mirror or prism

        I/O signals:
        O: synchronized   -- if true, laser is in sync and prism is rotating
        I: synchronize    -- activate synchronization
        I: exopose_start  -- start reading lines and exposing
        O: exopose_finish -- exposure is finished
        O: error          -- error signal
        O: lasers         -- laser pin
        O: pwm            -- pulse for scanner motor
        O: enable_prism   -- enable pin scanner motor
        I: photodiode     -- trigger for photodiode
        O: photodiode_t   -- high if photodiode triggered in this cycle
        O: read_commit    -- finalize read transactionalizedfifo
        O: read_en        -- enable read transactionalizedfifo
        I: read_data      -- read data from transactionalizedfifo
        I: empty          -- signal wether fifo is empty
        O: step           -- step signal
        O: direction      -- direction signal
    """
    def __init__(self, platform=None, top=False):
        '''
        top        -- trigger synthesis of module
        platform   -- pass test platform
        '''
        self.platform = platform
        self.status = Signal()
        self.lasers = Signal(2)
        self.pwm = Signal()
        self.enable_prism = Signal()
        self.synchronize = Signal()
        self.synchronized = Signal()
        self.error = Signal()
        self.photodiode = Signal()
        self.photodiode_t = Signal()
        self.read_commit = Signal()
        self.read_en = Signal()
        self.read_data = Signal(MEMWIDTH)
        self.read_discard = Signal()
        self.empty = Signal()
        self.expose_finished = Signal()
        self.expose_start = Signal()
        self.step = Signal()
        self.dir = Signal()
        self.dct = params(platform)

        
    def elaborate(self, platform):
        m = Module()
        if self.platform is not None:
            platform = self.platform

        dct = self.dct

        # Pulse generator for prism motor
        pwmcnt = Signal(range(dct['POLYPERIOD']))
        # photodiode_triggered
        photodiodecnt = Signal(range(dct['TICKSINFACET']*2))
        triggered = Signal()
        with m.If(photodiodecnt < (dct['TICKSINFACET']*2-1)):
            with m.If(self.photodiode):
                m.d.sync += triggered.eq(1)
            m.d.sync += photodiodecnt.eq(photodiodecnt+1)
        with m.Else():
            m.d.sync += [self.photodiode_t.eq(triggered),
                         photodiodecnt.eq(0),
                         triggered.eq(0)]
        
        # step generator
        move = Signal()
        stepcnt = Signal(56)
        stephalfperiod = Signal(55)
        with m.If(move):
            with m.If(stepcnt < stephalfperiod):
                m.d.sync += stepcnt.eq(stepcnt+1)
            with m.Else():
                m.d.sync += [stepcnt.eq(0),
                             self.step.eq(~self.step)]
        
        
        
        # pwm is always created but can be deactivated
        with m.If(pwmcnt == 0):
            m.d.sync += [self.pwm.eq(~self.pwm),
                         pwmcnt.eq(dct['POLYPERIOD']-1)]
        with m.Else():
            m.d.sync += pwmcnt.eq(pwmcnt-1)
        
        # Laser FSM
        facetcnt = Signal(range(dct['FACETS']))

        stablecntr = Signal(range(max(dct['SPINUPTICKS'], dct['STABLETICKS'])))
        stablethresh = Signal(range(dct['STABLETICKS']))
        lasercnt = Signal(range(dct['LASERTICKS']))
        scanbit = Signal(range(dct['BITSINSCANLINE']+1))
        tickcounter = Signal(range(dct['TICKSINFACET']*2))
        photodiode = self.photodiode
        read_data = self.read_data
        read_old = Signal.like(read_data)
        readbit = Signal(range(MEMWIDTH))
        photodiode_d = Signal()
        lasers = self.lasers
        if self.platform.name == 'Test':
            self.stephalfperiod = stephalfperiod
            self.tickcounter = tickcounter
            self.scanbit = scanbit
            self.lasercnt = lasercnt
            self.facetcnt = facetcnt

        # Exposure start detector
        process_lines = Signal()
        expose_start_d = Signal()

        m.d.sync += expose_start_d.eq(self.expose_start)
        with m.If((expose_start_d == 0) & self.expose_start):
            m.d.sync += [process_lines.eq(1),
                         self.expose_finished.eq(0)]

        with m.FSM(reset='RESET') as laserfsm:
            with m.State('RESET'):
                m.d.sync += self.error.eq(0)
                m.next = 'STOP'
            with m.State('STOP'):
                m.d.sync += [stablethresh.eq(dct['STABLETICKS']-1),
                             stablecntr.eq(0),
                             self.synchronized.eq(0),
                             self.enable_prism.eq(1),
                             readbit.eq(0),
                             facetcnt.eq(0),
                             scanbit.eq(0),
                             lasercnt.eq(0),
                             lasers.eq(0)]
                with m.If(self.synchronize&(~self.error)):
                    # laser is off, photodiode cannot be triggered
                    with m.If(self.photodiode == 0):
                        m.d.sync += self.error.eq(1)
                        m.next = 'STOP'
                    with m.Else():
                        m.d.sync += [self.error.eq(0),
                                     self.enable_prism.eq(0)]
                        m.next = 'SPINUP'
            with m.State('SPINUP'):
                with m.If(stablecntr > dct['SPINUPTICKS']-1):
                    # turn on laser
                    m.d.sync += [self.lasers.eq(int('1'*2, 2)),
                                 stablecntr.eq(0)]
                    m.next = 'WAIT_STABLE'
                with m.Else():
                    m.d.sync += stablecntr.eq(stablecntr+1)
            with m.State('WAIT_STABLE'):
                m.d.sync += photodiode_d.eq(photodiode)
                with m.If(stablecntr >= stablethresh):
                    m.d.sync += self.error.eq(1)
                    m.next = 'STOP'
                with m.Elif(~photodiode & ~photodiode_d):
                    m.d.sync += [tickcounter.eq(0),
                                 lasers.eq(0)]
                    with m.If((tickcounter > (dct['TICKSINFACET']-1)
                              - dct['JITTERTICKS']) &
                              (tickcounter < (dct['TICKSINFACET']-1)
                              + dct['JITTERTICKS'])):
                        m.d.sync += [stablecntr.eq(0),
                                     self.synchronized.eq(1),
                                     tickcounter.eq(0)]
                        with m.If(facetcnt == dct['FACETS']-1):
                            m.d.sync += facetcnt.eq(0)
                        with m.Else():
                            m.d.sync += facetcnt.eq(facetcnt+1)
                        with m.If(dct['SINGLE_FACET'] & (facetcnt > 0)):
                            m.d.sync += move.eq(0)
                            m.next = 'WAIT_END'
                        with m.Elif(self.empty | ~process_lines):
                            m.d.sync += move.eq(0)
                            m.next = 'WAIT_END'
                        with m.Else():
                            # TODO: 10 is too high, should be lower
                            thresh = min(round(10.1*dct['TICKSINFACET']),
                                         dct['STABLETICKS'])
                            m.d.sync += [stablethresh.eq(thresh),
                                         self.read_en.eq(1)]
                            m.next = 'READ_INSTRUCTION'
                    with m.Else():
                        m.d.sync += [move.eq(0),
                                     self.synchronized.eq(0)]
                        m.next = 'WAIT_END'
                with m.Else():
                    m.d.sync += [stablecntr.eq(stablecntr+1),
                                 tickcounter.eq(tickcounter+1)]
            with m.State('READ_INSTRUCTION'):
                m.d.sync += [self.read_en.eq(0), tickcounter.eq(tickcounter+1)]
                with m.If(read_data[0:8] == INSTRUCTIONS.SCANLINE):
                    m.d.sync += [move.eq(1),
                                 self.dir.eq(read_data[8]),
                                 stephalfperiod.eq(read_data[9:])]
                    m.next = 'WAIT_FOR_DATA_RUN'
                with m.Elif(read_data == INSTRUCTIONS.LASTSCANLINE):
                    m.d.sync += [self.expose_finished.eq(1),
                                 move.eq(0),
                                 self.read_commit.eq(1),
                                 process_lines.eq(0)]
                    m.next = 'WAIT_END'
                with m.Else():
                    m.d.sync += self.error.eq(1)
                    m.next = 'READ_INSTRUCTION'
            with m.State('WAIT_FOR_DATA_RUN'):
                m.d.sync += [tickcounter.eq(tickcounter+1),
                             readbit.eq(0),
                             scanbit.eq(0),
                             lasercnt.eq(0)]
                tickcnt_thresh = int(dct['START%']*dct['TICKSINFACET'])
                assert tickcnt_thresh > 0
                with m.If(tickcounter >= tickcnt_thresh):
                    m.d.sync += self.read_en.eq(1)
                    m.next = 'DATA_RUN'
            with m.State('DATA_RUN'):
                m.d.sync += tickcounter.eq(tickcounter+1)
                # NOTE:
                #      readbit is your current position in memory
                #      scanbit current byte position in scanline
                #      lasercnt used to pulse laser at certain freq
                with m.If(lasercnt == 0):
                    with m.If(scanbit >= dct['BITSINSCANLINE']):
                        with m.If(dct['SINGLE_LINE'] & self.empty):
                            m.d.sync += self.read_discard.eq(1)
                        with m.Else():
                            m.d.sync += self.read_commit.eq(1)
                        m.next = 'WAIT_END'
                    with m.Else():
                        m.d.sync += [lasercnt.eq(dct['LASERTICKS']-1),
                                     scanbit.eq(scanbit+1)]
                        with m.If(readbit == 0):
                            m.d.sync += [self.lasers[0].eq(self.read_data[0]),
                                         read_old.eq(self.read_data >> 1),
                                         self.read_en.eq(0)]
                        with m.Else():
                            m.d.sync += self.lasers[0].eq(read_old[0])
                with m.Else():
                    m.d.sync += lasercnt.eq(lasercnt-1)
                    # NOTE: read enable can only be high for 1 cycle
                    #       as a result this is done right be fore the "read"
                    with m.If(lasercnt == 1):
                        with m.If(readbit == 0 ):
                                m.d.sync += [readbit.eq(readbit+1)]
                        # final read bit copy memory
                        # move to next address, i.e. byte, if end is reached
                        with m.Elif(readbit == MEMWIDTH-1):
                            # If fifo is empty it will give errors later
                            # so it can be ignored here
                            # Only grab a new line if more than current
                            # is needed
                            # -1 as counting in python is different
                            with m.If(scanbit < (dct['BITSINSCANLINE'])):
                                m.d.sync += self.read_en.eq(1)
                            m.d.sync += readbit.eq(0)
                        with m.Else():
                            m.d.sync += [readbit.eq(readbit+1),
                                         read_old.eq(read_old >> 1)]
            with m.State('WAIT_END'):
                m.d.sync += [stablecntr.eq(stablecntr+1),
                             tickcounter.eq(tickcounter+1)]

                with m.If(dct['SINGLE_LINE'] & self.empty):
                    m.d.sync += self.read_discard.eq(0)
                with m.Else():
                    m.d.sync += self.read_commit.eq(0)
                # -1 as you count till range-1 in python
                # -2 as yuu need 1 tick to process
                with m.If(tickcounter >= round(dct['TICKSINFACET']
                          - dct['JITTERTICKS']-2)):
                    m.d.sync += lasers.eq(int('11', 2))
                    m.next = 'WAIT_STABLE'
                with m.Elif(~self.synchronize):
                    m.next = 'STOP'
        if self.platform.name == 'Test':
            self.laserfsm = laserfsm
        return m


class DiodeSimulator(Laserhead):
    """ Wraps laser head with object to simulate photodiode

        This is purely used for testing. Photodiode is only created
        if prism motor is enabled and the laser is on so the diode
        can be triggered.
    """
    def __init__(self, platform=None, top=False, laser_var=None):
        if laser_var is not None:
            platform.laser_var = laser_var
        super().__init__(platform, top=False)
        self.write_en = Signal()
        self.write_commit = Signal()
        self.write_data = Signal(MEMWIDTH)

    def elaborate(self, platform):
        if self.platform is not None:
            platform = self.platform
        m = super().elaborate(platform)

        dct = self.dct
        diodecounter = Signal(range(dct['TICKSINFACET']))
        self.diodecounter = diodecounter

        fifo = TransactionalizedFIFO(width=MEMWIDTH,
                                     depth=platform.memdepth)
        m.submodules.fifo = fifo
        self.fifo = fifo
        m.d.comb += [fifo.write_data.eq(self.write_data),
                     fifo.write_commit.eq(self.write_commit),
                     fifo.write_en.eq(self.write_en),
                     fifo.read_commit.eq(self.read_commit),
                     fifo.read_en.eq(self.read_en),
                     self.empty.eq(fifo.empty),
                     fifo.read_discard.eq(self.read_discard),
                     self.read_data.eq(fifo.read_data)]

        with m.If(diodecounter == (dct['TICKSINFACET']-1)):
            m.d.sync += diodecounter.eq(0)
        with m.Elif(diodecounter > (dct['TICKSINFACET']-4)):
            m.d.sync += [self.photodiode.eq(~((~self.enable_prism)
                                            & (self.lasers > 0))),
                         diodecounter.eq(diodecounter+1)]
        with m.Else():
            m.d.sync += [diodecounter.eq(diodecounter+1),
                         self.photodiode.eq(1)]
        self.diodecounter = diodecounter
        return m


class BaseTest(LunaGatewareTestCase):
    'Base class for laserhead test'

    def initialize_signals(self):
        '''If not triggered the photodiode is high'''
        yield self.dut.photodiode.eq(1)
        self.host = controller.Host(self.platform)

    def getState(self, fsm=None):
        if fsm is None:
            fsm = self.dut.laserfsm
        return fsm.decoding[(yield fsm.state)]
    
    def count_steps(self):
        '''counts steps in accounting for direction
        
        Very similar to the function in movement.py
        '''
        count = 0
        dut = self.dut
        while ((yield dut.expose_finished)==0):
            old = (yield self.dut.step)
            yield
            if old and ((yield self.dut.step) == 0):
                if (yield self.dut.dir):
                    count += 1
                else:
                    count -= 1
        return count

    def waituntilState(self, state, fsm=None):
        dut = self.dut
        timeout = max(dut.dct['TICKSINFACET']*2, dut.dct['STABLETICKS'],
                      dut.dct['SPINUPTICKS'])
        count = 0
        while (yield from self.getState(fsm)) != state:
            yield
            count += 1
            if count > timeout:
                print(f"Did not reach {state} in {timeout} ticks")
                self.assertTrue(count < timeout)

    def assertState(self, state, fsm=None):
        self.assertEqual(self.getState(state), state)

    def checkline(self, bitlst, stepsperline=1, direction=0):
        'it is verified wether the laser produces the pattern in bitlist'
        dut = self.dut
        if not dut.dct['SINGLE_LINE']:
            self.assertEqual((yield dut.empty), False)
        yield from self.waituntilState('READ_INSTRUCTION')
        yield
        self.assertEqual((yield dut.dir), direction)
        if len(bitlst) != 0:
            self.assertEqual((yield dut.stephalfperiod),
                             round(stepsperline*dut.dct['TICKSINFACET']/2))
        self.assertEqual((yield dut.error), False)
        if len(bitlst) == 0:
            self.assertEqual((yield dut.error), False)
            self.assertEqual((yield dut.expose_finished), True)
        else:
            yield from self.waituntilState('DATA_RUN')
            yield
            for idx, bit in enumerate(bitlst):
                assert (yield dut.lasercnt) == dut.dct['LASERTICKS']-1
                assert (yield dut.scanbit) == idx+1
                for _ in range(dut.dct['LASERTICKS']):    
                    assert (yield dut.lasers[0]) == bit
                    yield
        if (len(bitlst) == 0) & ((yield dut.synchronize) == 0):
            yield from self.waituntilState('STOP')
        else:
            yield from self.waituntilState('WAIT_END')
        self.assertEqual((yield self.dut.error), False)

    def write_line(self, bitlist, stepsperline=1, direction=0):
        '''write line to fifo

        This is a helper function to allow testing of the module
        without dispatcher and parser
        '''
        bytelst = self.host.bittobytelist(bitlist, stepsperline, direction)
        dut = self.dut
        for i in range(0, len(bytelst), WORD_BYTES):
            lst = bytelst[i:i+WORD_BYTES]
            number = unpack('Q', bytearray(lst))[0]
            yield dut.write_data.eq(number)
            yield from self.pulse(dut.write_en)
        yield from self.pulse(dut.write_commit)

    def scanlineringbuffer(self, numblines=3):
        'write several scanlines and verify receival'
        dut = self.dut
        lines = []
        for _ in range(numblines):
            line = []
            for _ in range(dut.dct['SCANBITS']):
                line.append(randint(0, 1))
            lines.append(line)
        lines.append([])
        for line in lines:
            yield from self.write_line(line)
        yield from self.pulse(dut.expose_start)
        yield dut.synchronize.eq(1)
        for line in lines:
            yield from self.checkline(line)
        self.assertEqual((yield dut.empty), True)
        self.assertEqual((yield dut.expose_finished), True)


class LaserheadTest(BaseTest):
    'Test laserhead without triggering photodiode'

    platform = TestPlatform()
    FRAGMENT_UNDER_TEST = Laserhead
    FRAGMENT_ARGUMENTS = {'platform': platform}

    @sync_test_case
    def test_pwmpulse(self):
        '''pwm pulse generation test'''
        dut = self.dut
        dct = params(self.platform)
        while (yield dut.pwm) == 0:
            yield
        cnt = 0
        while (yield dut.pwm) == 1:
            cnt += 1
            yield
        self.assertEqual(cnt,
                         int(dct['CRYSTAL_HZ']/(dct['POLY_HZ']*6*2))
                         )

    @sync_test_case
    def test_sync(self):
        '''error is raised if laser not synchronized'''
        dut = self.dut
        yield dut.synchronize.eq(1)
        yield from self.waituntilState('SPINUP')
        self.assertEqual((yield dut.error), 0)
        yield from self.waituntilState('WAIT_STABLE')
        yield from self.waituntilState('STOP')
        self.assertEqual((yield dut.error), 1)


class SinglelineTest(BaseTest):
    'Test laserhead while triggering photodiode and single line'
    platform = TestPlatform()
    laser_var = deepcopy(platform.laser_var)
    laser_var['SINGLE_FACET'] = True
    laser_var['SINGLE_LINE'] = True
    FRAGMENT_UNDER_TEST = DiodeSimulator
    FRAGMENT_ARGUMENTS = {'platform': platform,
                          'laser_var': laser_var}

    @sync_test_case
    def test_single_line(self):
        dut = self.dut
        lines = [[1, 1]]
        for line in lines:
            yield from self.write_line(line)
        yield dut.synchronize.eq(1)
        yield from self.pulse(dut.expose_start)
        for _ in range(10):
            yield from self.checkline(line)
        lines = [[1, 0], []]
        self.assertEqual((yield dut.expose_finished), 0)
        for line in lines:
            yield from self.write_line(line)
        # the last line, i.e. [], triggers exposure finished
        while (yield dut.expose_finished) == 0:
            yield
        self.assertEqual((yield dut.expose_finished), 1)
        yield dut.synchronize.eq(0)
        yield from self.waituntilState('STOP')
        self.assertEqual((yield dut.error), False)


class SinglelinesinglefacetTest(BaseTest):
    '''Test laserhead while triggering photodiode.

        Laserhead is in single line and single facet mode'''
    platform = TestPlatform()
    laser_var = deepcopy(platform.laser_var)
    laser_var['SINGLE_FACET'] = True
    laser_var['SINGLE_LINE'] = True
    FRAGMENT_UNDER_TEST = DiodeSimulator
    FRAGMENT_ARGUMENTS = {'platform': platform,
                          'laser_var': laser_var}

    @sync_test_case
    def test_single_line_single_facet(self):
        dut = self.dut
        lines = [[1, 1]]
        for line in lines:
            yield from self.write_line(line)
        yield dut.synchronize.eq(1)
        yield from self.pulse(dut.expose_start)
        # facet counter changes
        for facet in range(self.platform.laser_var['FACETS']-1):
            self.assertEqual(facet, (yield dut.facetcnt))
            yield from self.waituntilState('WAIT_STABLE')
            yield from self.waituntilState('WAIT_END')
        # still line only projected at specific facet count
        for _ in range(3):
            yield from self.checkline(line)
            self.assertEqual(1, (yield dut.facetcnt))
        self.assertEqual((yield dut.error), False)


class MultilineTest(BaseTest):
    'Test laserhead while triggering photodiode and ring buffer'
    platform = TestPlatform()
    FRAGMENT_UNDER_TEST = DiodeSimulator
    FRAGMENT_ARGUMENTS = {'platform': platform}

    @sync_test_case
    def test_sync(self):
        '''as photodiode is triggered wait end is reached
        '''
        dut = self.dut
        yield dut.synchronize.eq(1)
        yield from self.waituntilState('SPINUP')
        self.assertEqual((yield dut.error), 0)
        for _ in range(3):
            yield from self.waituntilState('WAIT_STABLE')
            yield from self.waituntilState('WAIT_END')
        self.assertEqual((yield dut.error), 0)
        
    @sync_test_case
    def test_stopline(self):
        'verify data run is not reached when stopline is sent'
        line = []
        dut = self.dut
        yield from self.write_line(line)
        yield dut.synchronize.eq(1)
        yield from self.pulse(dut.expose_start)
        self.assertEqual((yield dut.empty), 0)
        yield from self.waituntilState('SPINUP')
        yield dut.synchronize.eq(0)
        yield from self.checkline(line)
        self.assertEqual((yield dut.expose_finished), True)
        # to ensure it stays finished
        yield
        yield
        self.assertEqual((yield dut.expose_finished), True)
        self.assertEqual((yield dut.empty), True)
    
    @sync_test_case
    def test_movement(self, numblines=3, stepsperline=1):
        '''verify scanhead moves as expected forward / backward
        
        stepsperline  -- number of steps per line
        direction     -- 0 is negative and 1 is positive
        '''
        dut = self.dut
        def domove(direction):
            lines = [[1]*dut.dct['BITSINSCANLINE']]*numblines
            lines.append([])
            for line in lines:
                yield from self.write_line(line, stepsperline, direction)
            yield dut.synchronize.eq(1)
            yield from self.pulse(dut.expose_start)
            steps = (yield from self.count_steps())
            if not direction:
                direction = -1
            self.assertEqual(steps, stepsperline*numblines*direction)
        yield from domove(0)
        yield from domove(1)
        
        
    @sync_test_case
    def test_scanlineringbuffer(self, numblines=3):
        'write several scanlines and verify receival'
        yield from self.scanlineringbuffer(numblines=numblines)


class Loweredge(BaseTest):
    'Test Scanline of length MEMWDITH'
    platform = TestPlatform()
    FRAGMENT_UNDER_TEST = DiodeSimulator
    
    dct = deepcopy(platform.laser_var)
    dct['TICKSINFACET'] = 500
    dct['LASERTICKS'] = 3
    dct['SINGLE_LINE'] = False
    dct['SCANBITS'] = MEMWIDTH
    FRAGMENT_ARGUMENTS = {'platform': platform,
                          'laser_var': dct}
    
    @sync_test_case
    def test_scanlineringbuffer(self, numblines=3):
        'write several scanlines and verify receival'
        yield from self.scanlineringbuffer(numblines=numblines)


class Upperedge(Loweredge):
    platform = TestPlatform()
    FRAGMENT_UNDER_TEST = DiodeSimulator
    dct = deepcopy(platform.laser_var)
    dct['TICKSINFACET'] = 500
    dct['LASERTICKS'] = 3
    dct['SINGLE_LINE'] = False
    dct['SCANBITS'] = MEMWIDTH+1
    FRAGMENT_ARGUMENTS = {'platform': platform,
                          'laser_var': dct}


if __name__ == "__main__":
    unittest.main()

# NOTE: new class is created to reset settings
#       couldn't avoid this easily so kept for now

#  verify that you don't move after stop

#  verify that if you don't sent out a line, e.g. communication error that you don't  move
#  verify the above for the single facet mode
#  if you dont'get a laser trigger OUT OF count ... should stop move and reset count to 0
