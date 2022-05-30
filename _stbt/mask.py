from dataclasses import dataclass

import numpy

from .types import Region


class Mask:
    def __init__(self, m, *, invert=False):
        """Private constructor; for public use see `load_mask`."""
        # One (and only one) of these will be set: image, binop, region.
        self._image = None
        self._binop = None
        self._region = None
        if isinstance(m, (str, numpy.ndarray)):
            from .imgutils import load_image
            self._image = load_image(m, color_channels=(1, 3))
            self._invert = invert
        elif isinstance(m, BinOp):
            self._binop = m
            self._invert = invert
        elif isinstance(m, Mask):
            self._image = m._image
            self._binop = m._binop
            self._region = m._region
            self._invert = m._invert
            if invert:
                self._invert = not self._invert
        elif isinstance(m, Region):
            self._region = m
            self._invert = invert
        elif m is None:  # Region.intersect can return None for "no region"
            if invert:
                self._region = Region.ALL
            else:
                self._region = None
            self._invert = False
        else:
            raise TypeError("Expected filename, Image, Mask, or Region. "
                            f"Got {m!r}")

        # for memoisation:
        self._array = None

    def to_array(self, shape):
        if self._array is not None and self._array.shape == shape:
            return self._array

        if self._image is not None:
            array = self._image
            if array.shape[:2] != shape[:2]:
                raise ValueError(f"Mask shape {array.shape} and required shape "
                                 f"{shape} don't match")
            if array.shape[2] != shape[2]:
                from .imgutils import load_image
                array = load_image(array, color_channels=shape[2])
        elif self._binop is not None:
            n = self._binop
            if n.op == "+":
                array = n.left.to_array(shape) | n.right.to_array(shape)
            elif n.op == "-":
                array = n.left.to_array(shape) & ~n.right.to_array(shape)
            else:
                assert False, f"Unreachable: Unknown op {n.op}"
        else:  # Region (including None)
            array = numpy.full(shape, 0, dtype=numpy.uint8)
            r = Region.intersect(self._region, Region(0, 0, shape[1], shape[0]))
            if r:
                array[r.y:r.bottom, r.x:r.right] = 255

        if self._invert:
            array = ~array  # pylint:disable=invalid-unary-operand-type

        self._array = array
        return self._array

    def __repr__(self):
        # In-order traversal, removing unnecessary parentheses.
        prefix = "~" if self._invert else ""
        if self._image is not None:
            if self._image.relative_filename:
                return f"{prefix}Mask({self._image.relative_filename!r})"
            else:
                return f"{prefix}Mask(<Image>)"
        elif self._binop is not None:
            left_repr = repr(self._binop.left)
            right_repr = repr(self._binop.right)
            if "-" in right_repr and right_repr[0] not in ("~", "("):
                right_repr = f"({right_repr})"
            if prefix:
                open_paren, close_paren = "(", ")"
            else:
                open_paren, close_paren = "", ""
            return (f"{prefix}{open_paren}{left_repr} {self._binop.op} "
                    f"{right_repr}{close_paren}")
        elif self._region is not None:
            return f"{prefix}{self._region!r}"
        else:
            assert False, "Unreachable: Logic error in recursion"

    def __add__(self, other):
        if isinstance(other, (Region, Mask)):
            return Mask(BinOp("+", self, Mask(other)))
        else:
            return NotImplemented

    def __sub__(self, other):
        if isinstance(other, (Region, Mask)):
            return Mask(BinOp("-", self, Mask(other)))
        else:
            return NotImplemented

    def __invert__(self):
        return Mask(self, invert=True)


@dataclass
class BinOp:
    op: str  # "+" or "-"
    left: Mask
    right: Mask
