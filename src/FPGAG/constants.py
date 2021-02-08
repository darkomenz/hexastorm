from collections import namedtuple

COMMANDS = namedtuple('COMMANDS', ['EMPTY', 'GCODE', 'EXIT', 'ABORT'],
                      defaults=range(4))()
# one block is 4K there are 32 blocks (officially 20 in HX4K)
# max of 1 block is 16*256 but if you use a larger memwidth you seem to
# use more blocks so this might work
MEMWIDTH = 32
MEMDEPTH = 256
COMMAND_SIZE =8
WORD_SIZE = 32
WORD_BYTES = round(WORD_SIZE/8)
G_CODE = {'COMMAND': 1,
          'DIRECTION': 1,
          'LOOPS_ACCEL': 2,
          'LOOPS_TRAVEL': 2,
          'LOOPS_DECEL': 2,
          'AUX': 2,
          'ACCEL_SERIES_INDEX': 4,
          'HIRES_ACCEL_CYCLES': 4,
          'TRAVEL_DELAY_CYCLES': 4,
          'FRACTIONS': 4,
          'JERK_START': 2,
          'JERK_STOP': 2
}
QUEUE_ELEMENT_BYTES = sum(G_CODE.values())-G_CODE['COMMAND']
BYTESINGCODE = QUEUE_ELEMENT_BYTES+(WORD_BYTES-QUEUE_ELEMENT_BYTES%WORD_BYTES)

VARIABLES = {'CRYSTAL_HZ': 50E6}


# NOTE: following doesnt work due to bug in pylint https://github.com/PyCQA/pylint/issues/3876
# def customnamedtuple(typename, field_names) -> namedtuple:
#    return namedtuple(typename, field_names,
#                      defaults=range(len(field_names)))
