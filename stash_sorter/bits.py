"""Little-endian, LSB-first bit access as used by Diablo II item data."""


class BitReader:
    def __init__(self, data, pos_bits=0):
        self.data = data
        self.pos = pos_bits
        self.limit = len(data) * 8

    def read(self, n):
        if n == 0:
            return 0
        start = self.pos
        end = start + n
        if end > self.limit:
            raise EOFError(f"read past end of data (bit {start} + {n} > {self.limit})")
        chunk = int.from_bytes(self.data[start >> 3:(end + 7) >> 3], "little")
        self.pos = end
        return (chunk >> (start & 7)) & ((1 << n) - 1)

    def align(self):
        self.pos = (self.pos + 7) & ~7

    @property
    def byte_pos(self):
        return self.pos >> 3


def get_bits(buf, bitpos, n):
    chunk = int.from_bytes(buf[bitpos >> 3:(bitpos + n + 7) >> 3], "little")
    return (chunk >> (bitpos & 7)) & ((1 << n) - 1)


def set_bits(buf: bytearray, bitpos, n, value):
    if value < 0 or value >= (1 << n):
        raise ValueError(f"value {value} does not fit in {n} bits")
    for i in range(n):
        b = bitpos + i
        if (value >> i) & 1:
            buf[b >> 3] |= 1 << (b & 7)
        else:
            buf[b >> 3] &= ~(1 << (b & 7)) & 0xFF
