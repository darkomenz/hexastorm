from struct import unpack

import numpy as np
from gpiozero import LED

from FPGAG.constants import (INSTRUCTIONS, COMMANDS, FREQ, STATE,
                             MOVE_TICKS, WORD_BYTES, COMMAND_BYTES)
from FPGAG.board import Firestarter


class Host:
    'Class for sending instructions to core'

    def __init__(self, board=None):
        if board is None:
            self.board = Firestarter()
        else:
            self.board = board
        self._positions = [0]*board.motors

    def _read_state(self):
        '''reads the state and returns bits'''
        read_data = (yield from self.send_command([COMMANDS.READ] +
                                                  WORD_BYTES*[0]))
        return "{:08b}".format(read_data)

    @property
    def positions(self):
        '''retrieves and updates position'''
        for i in range(self.board.motors):
            read_data = (yield from self.send_command([COMMANDS.POSITION] +
                                                      WORD_BYTES*[0],
                                                      format='!q'))
            self._positions[i] = read_data
        return self._positions

    @property
    def pinstate(self):
        '''retrieves pin state as dictionary'''
        bits = (yield from self._read_state())
        dct = {'x': int(bits[0]),
               'y': int(bits[1]),
               'z': int(bits[2])}
        return dct

    @property
    def dispatcherror(self):
        '''retrieves dispatch error status of FPGA via SPI'''
        bits = self._read_state()
        return int(bits[-STATE.DISPATCHERROR-1])

    @property
    def enable_steppers(self):
        '''get status enables pin motors

        Execution might still be disabled on the FPGA
        '''
        enable = LED(self.board.enable_pin)
        return enable.value

    @enable_steppers.setter
    def enable_steppers(self, val):
        '''disable stepper motors

        sets enable pin on raspberry pi board
        send enable or disable command to FPGA core

        val -- boolean, True enables steppers
        '''
        assert type(val) == bool
        enable = LED(self.board.enable_pin)
        if val:
            enable.on()
            yield from self.spi_exchange_data([COMMANDS.ENABLE]+3*[0])
        else:
            enable.off()
            yield from self.spi_exchange_data([COMMANDS.DISABLE]+3*[0])

    @property
    def execution(self):
        '''determine wether code in SRAM is dispatched

        The dispachter on the FPGA can be on or off
        '''
        bits = self._read_state()
        return int(bits[-STATE.PARSING-1])

    def _executionsetter(self, val):
        'not able to call execution with yield from'
        assert type(val) == bool
        command = []
        if val:
            command.append(COMMANDS.START)
        else:
            command.append(COMMANDS.STOP)
        yield from self.send_command(command+WORD_BYTES*[0])

    @execution.setter
    def execution(self, val):
        '''set dispatcher on or of

        val -- True, dispatcher is enabled
        '''
        self._executionsetter(val)

    def home_axes(axes, speed):
        '''home given axes

        axes  -- list with axes numbers to home
        speed -- speed in mm/s used to home
        '''
        pass

    def gotopoint(self, position, absolute=True,
                  speed=None):
        '''move steppers to point with constant speed

        postion      -- list with coordinate or distance in mm
        absolute     -- absolute position otherwise coordinate is distance
        speed        -- speed in mm/s
        '''
        assert len(position) == self.board.motors
        if speed is not None:
            assert len(speed) == self.board.motors
        else:
            speed = np.array([10, 10, 10])
        if absolute:
            dist = np.array([position]) - self.position
        else:
            dist = np.array([position])
        t = dist/speed
        ticks = t*FREQ

        def get_ticks(x):
            if x >= MOVE_TICKS:
                return MOVE_TICKS
            else:
                return x
        get_ticks_v = np.vectorize(get_ticks)
        while ticks != np.array([0, 0, 0]):
            ticks_move = get_ticks_v(ticks)
            ticks = ticks - MOVE_TICKS
            ticks[ticks < 0] = 0
            data = self.move_data(ticks_move.to_list(), speed.to_list(),
                                  [0]*3, [0]*3)
            yield from self.send_move(data)

    def memfull(self, data):
        '''check if memory is full

        data -- data received from peripheral
        '''
        bits = "{:08b}".format(data)
        return int(bits[STATE.FULL])

    def send_command(self, data, format='!Q'):
        assert len(data) == WORD_BYTES+COMMAND_BYTES
        read_data = yield from self.spi_exchange_data(data)
        return unpack(format, read_data[1:])[0]

    def send_move(self, ticks, a, b, c, iterations=100):
        '''send move instruction with data

        data            -- coefficients for polynomal move
        iterations      -- number of trials
        This method is blocking and keeps sending data
        '''
        commands = self.move_commands(ticks, a, b, c)
        trials = 0
        data_out = 255
        while self.memfull(data_out):
            data_out = (yield from self.send_command(commands.pop(0)))
            trials += 1
            if trials > iterations:
                raise Exception("Too many trials needed")
        for command in commands:
            yield from self.send_command(command)

    def move_commands(self, ticks, a, b, c):
        '''get list of commands for move instruction with
           [a,b,c] for ax+bx^2+cx^3

           ticks        -- ticks in move
           speed        -- speed in mm/s
           acceleration -- acceleration in mm/s2
           postion      -- list with position in mm
        '''
        assert len(ticks) == len(a) == len(b) == len(c) == self.board.motors
        write_byte = COMMANDS.WRITE.to_bytes(1, 'big')
        move_byte = INSTRUCTIONS.MOVE.to_bytes(1, 'big')
        commands = []
        for motor in range(self.board.motors):
            commands += [write_byte +
                         ticks[motor].to_bytes(7, 'big') + move_byte]
            commands += [write_byte + a[motor].to_bytes(8, 'big', signed=True)]
            commands += [write_byte + b[motor].to_bytes(8, 'big', signed=True)]
            commands += [write_byte + c[motor].to_bytes(8, 'big', signed=True)]
        return commands

    def spi_exchange_data(data):
        '''writes data to peripheral, returns reply'''
        pass
