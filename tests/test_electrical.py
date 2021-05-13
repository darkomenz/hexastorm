import unittest
from time import sleep
from math import floor

import numpy as np
from numpy.testing import assert_array_equal

from hexastorm.controller import Host, Memfull, executor
from hexastorm.platforms import Firestarter
from hexastorm.constants import (WORD_BYTES, COMMANDS, MOVE_TICKS,
                                 wordsinmove)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls, flash=True):
        cls.host = Host()
        if flash:
            cls.host.build()
        else:
            print("Resetting the machine")
            cls.host.reset()


class StaticTest(Base):
    @executor
    def test_memfull(self):
        '''test if memory can be filled and emptied

        Yosys does not raise error for memories which cannot be synthesized
        You fill the entire memory and see if it is properly emptied
        by ensuring that the position changes as excepted.
        '''
        yield from self.host._executionsetter(False)
        self.host.enable_steppers = False
        # with false check if this results in block
        # you can do a blink test to verify move
        motors = Firestarter.motors
        host = self.host
        mm = np.array([1]*motors)
        steps = mm * np.array(list(host.platform.stepspermm.values()))
        limit = floor(host.platform.memdepth /
                      wordsinmove(host.platform.motors))
        for _ in range(host.platform.memdepth):
            a = ((host.steps_to_count(steps.astype('int64'))/MOVE_TICKS)
                 .round().astype('int64'))
            try:
                yield from host.send_move([MOVE_TICKS],
                                          a.tolist(),
                                          [0]*motors,
                                          [0]*motors, maxtrials=10)
            except Memfull:
                pass
        self.assertEqual((yield from self.host.memfull()), True)
        yield from self.host._executionsetter(True)
        sleep(1)
        self.assertEqual((yield from self.host.memfull()), False)
        assert_array_equal((yield from self.host.position),
                           mm*limit)
        self.assertEqual((yield from self.host.error), False)
        self.host.reset()

    @executor
    def test_invalidinstruction(self):
        '''write invalid instruction and verify it passes dispatcher'''
        command = [COMMANDS.WRITE] + [0]*WORD_BYTES
        for _ in range(wordsinmove(self.host.platform.motors)):
            yield from self.host.send_command(command)
        sleep(3)
        self.assertEqual((yield from self.host.memfull()), False)
        self.assertEqual((yield from self.host.error), True)
        self.host.reset()


class LaserheadTest(Base):
    def test_setlaserpower(self, power=100):
        '''test if laser power can be set

        sets value on digipot via I2C
        and verifies this is equal to readed value of digipot
        '''
        self.host.laser_power = power
        assert self.host.laser_power == power

    @executor
    def spinprism(self, timeout=3):
        'spin for timeout seconds'
        yield from self.host.enable_comp(polygon=True)
        print(f'Spinning prism for {timeout} seconds')
        sleep(timeout)
        yield from self.host.enable_comp(polygon=False)
        self.assertEqual((yield from self.host.error), False)

    @executor
    def lasertest(self, timeout=3):
        'enable laser for timeout seconds'
        yield from self.host.enable_comp(laser1=True)
        print(f'Laser on for {timeout} seconds')
        sleep(timeout)
        yield from self.host.enable_comp(laser0=False)
        self.assertEqual((yield from self.host.error), False)

    @executor
    def test_diode(self, timeout=3):
        'enable motor, laser and verify photodiode is triggered'
        res = (yield from self.host.pinstate)['photodiode_trigger']
        self.assertEqual(res, 0)
        yield from self.host.enable_comp(laser1=True, polygon=True)
        print(f'Wait for diode trigger, {timeout} seconds')
        sleep(timeout)
        res = (yield from self.host.pinstate)['photodiode_trigger']
        yield from self.host.enable_comp(laser1=False, polygon=False)
        self.assertEqual(res, 1)
        self.assertEqual((yield from self.host.error), False)

    @executor
    def test_stable(self, timeout=3):
        yield from self.host.enable_comp(synchronize=True)
        print(f'Wait for synchronization, {timeout} seconds')
        sleep(timeout)
        res = (yield from self.host.pinstate)['photodiode_trigger']
        self.assertEqual(res, 1)
        yield from self.host.enable_comp(synchronize=False)
        self.assertEqual((yield from self.host.error), False)

    @executor
    def test_move(self, dist=10, stepsperline=1, timeout=3):
        '''verifies movement during a forward and backward 
           scanning move

        dist    -- distance in mm
        timeout -- wait
        '''
        host = self.host
        laser_params  = host.laser_params
        numblines = round(dist*host.platform.stepspermm[host.platform.laser_axis]
                    *stepsperline)
        estimated_time = numblines/(laser_params['FACETS']*laser_params['RPM']/60)
        yield from self.host.enable_comp(synchronize=True)
        self.host.enable_steppers = True
        print(f'Wait for synchronization, {timeout} seconds')
        sleep(timeout)
        line = [0] * laser_params['BITSINSCANLINE']
        for direction in [0, 1]:
            for _ in range(numblines):
                yield from self.host.writeline(line, stepsperline, direction)
            print('switching direction')
        yield from host.writeline([])
        print(f'Wait for stopline to execute, {timeout} seconds')
        sleep(timeout)
        self.assertEqual((yield from host.error), False)
        yield from host.enable_comp(synchronize=False)
        self.host.enable_steppers = False

    @executor
    def test_scanline(self, timeout=3, numblines=1000):
        line = [1] * self.host.laser_params['BITSINSCANLINE']
        for _ in range(numblines):
            yield from self.host.writeline(line)
        yield from self.host.writeline([])
        print(f'Wait for stopline to execute, {timeout} seconds')
        sleep(timeout)
        self.assertEqual((yield from self.host.error), False)
        yield from self.host.enable_comp(synchronize=False)


class MoveTest(Base):
    '''Test movement core'''

    @executor
    def readpin(self):
        '''test if you can detect triggers of the limit switches

        This is typically executed manually by placing a sheet of paper
        in the cavity of the optical switch.
        '''
        self.host.enable_steppers = False
        while True:
            print((yield from self.host.pinstate))
            sleep(1)

    @executor
    def motorenable(self):
        '''test if motors are enabled and execution is enabled/disabled
           via communication with FPGA'''
        self.host.enable_steppers = True
        self.assertEqual((yield from self.host.execution), True)
        print('check manually if axes are blocked and hard to move')
        input()
        self.host.enable_steppers = False
        self.assertEqual((yield from self.host.execution), False)

    @executor
    def multiplemove(self):
        '''test if motors move'''
        motors = Firestarter.motors
        position = np.array([10, 10, 0])
        self.assertEqual((yield from self.host.error), False)
        self.host.enable_steppers = True
        yield from self.host.gotopoint(position=position,
                                       speed=[1]*motors,
                                       absolute=False)
        # NOTE: is the sleep really needed?
        print('sleep to complete')
        sleep(1)
        assert_array_equal((yield from self.host.position),
                           position)
        self.host.enable_steppers = False


if __name__ == "__main__":
    unittest.main()