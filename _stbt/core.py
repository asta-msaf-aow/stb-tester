# coding: utf-8
"""Main stb-tester python module. Intended to be used with `stbt run`.

See `man stbt` and http://stb-tester.com for documentation.

Copyright 2012-2013 YouView TV Ltd and contributors.
License: LGPL v2.1 or (at your option) any later version (see
https://github.com/stb-tester/stb-tester/blob/master/LICENSE for details).
"""

from __future__ import absolute_import

import argparse
import datetime
import functools
import inspect
import itertools
import os
import sys
import threading
import traceback
import warnings
import weakref
from collections import deque, namedtuple
from contextlib import contextmanager
from textwrap import dedent

import cv2
import gi
import numpy

import _stbt.cv2_compat as cv2_compat
from _stbt import imgproc_cache, logging
from _stbt.config import ConfigurationError, get_config
from _stbt.gst_utils import (array_from_sample, gst_iterate,
                             gst_sample_make_writable)
from _stbt.imgutils import (_frame_repr, _image_region, _ImageFromUser,
                            _load_image, crop, find_user_file, Frame)
from _stbt.logging import ddebug, debug, draw_on, warn
from _stbt.types import Region, UITestError, UITestFailure

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # isort:skip pylint: disable=E0611

Gst.init(None)

warnings.filterwarnings(
    action="always", category=DeprecationWarning, message='.*stb-tester')


# Functions available to stbt scripts
# ===========================================================================


class MatchParameters(object):
    """Parameters to customise the image processing algorithm used by
    `match`, `wait_for_match`, and `press_until_match`.

    You can change the default values for these parameters by setting a key
    (with the same name as the corresponding python parameter) in the
    ``[match]`` section of :ref:`.stbt.conf`. But we strongly recommend that
    you don't change the default values from what is documented here.

    You should only need to change these parameters when you're trying to match
    a template image that isn't actually a perfect match -- for example if
    there's a translucent background with live TV visible behind it; or if you
    have a template image of a button's background and you want it to match even
    if the text on the button doesn't match.

    :param str match_method:
      The method to be used by the first pass of stb-tester's image matching
      algorithm, to find the most likely location of the "template" image
      within the larger source image.

      Allowed values are "sqdiff-normed", "ccorr-normed", and "ccoeff-normed".
      For the meaning of these parameters, see OpenCV's
      :ocv:pyfunc:`cv2.matchTemplate`.

      We recommend that you don't change this from its default value of
      "sqdiff-normed".

    :param float match_threshold:
      How strong a result from the first pass must be, to be considered a
      match. Valid values range from 0 (anything is considered to match)
      to 1 (the match has to be pixel perfect). This defaults to 0.8.

    :param str confirm_method:
      The method to be used by the second pass of stb-tester's image matching
      algorithm, to confirm that the region identified by the first pass is a
      good match.

      The first pass often gives false positives (it reports a "match" for an
      image that shouldn't match). The second pass is more CPU-intensive, but
      it only checks the position of the image that the first pass identified.
      The allowed values are:

      :"none":
        Do not confirm the match. Assume that the potential match found is
        correct.

      :"absdiff":
        Compare the absolute difference of each pixel from the template image
        against its counterpart from the candidate region in the source video
        frame.

      :"normed-absdiff":
        Normalise the pixel values from both the template image and the
        candidate region in the source video frame, then compare the absolute
        difference as with "absdiff".

        This gives better results with low-contrast images. We recommend setting
        this as the default `confirm_method` in stbt.conf, with a
        `confirm_threshold` of 0.30.

    :param float confirm_threshold:
      The maximum allowed difference between any given pixel from the template
      image and its counterpart from the candidate region in the source video
      frame, as a fraction of the pixel's total luminance range.

      Valid values range from 0 (more strict) to 1.0 (less strict).
      Useful values tend to be around 0.16 for the "absdiff" method, and 0.30
      for the "normed-absdiff" method.

    :param int erode_passes:
      After the "absdiff" or "normed-absdiff" absolute difference is taken,
      stb-tester runs an erosion algorithm that removes single-pixel differences
      to account for noise. Useful values are 1 (the default) and 0 (to disable
      this step).

    """

    def __init__(self, match_method=None, match_threshold=None,
                 confirm_method=None, confirm_threshold=None,
                 erode_passes=None):
        if match_method is None:
            match_method = get_config('match', 'match_method')
        if match_threshold is None:
            match_threshold = get_config(
                'match', 'match_threshold', type_=float)
        if confirm_method is None:
            confirm_method = get_config('match', 'confirm_method')
        if confirm_threshold is None:
            confirm_threshold = get_config(
                'match', 'confirm_threshold', type_=float)
        if erode_passes is None:
            erode_passes = get_config('match', 'erode_passes', type_=int)

        if match_method not in (
                "sqdiff-normed", "ccorr-normed", "ccoeff-normed"):
            raise ValueError("Invalid match_method '%s'" % match_method)
        if confirm_method not in ("none", "absdiff", "normed-absdiff"):
            raise ValueError("Invalid confirm_method '%s'" % confirm_method)

        self.match_method = match_method
        self.match_threshold = match_threshold
        self.confirm_method = confirm_method
        self.confirm_threshold = confirm_threshold
        self.erode_passes = erode_passes

    def __repr__(self):
        return (
            "MatchParameters(match_method=%r, match_threshold=%r, "
            "confirm_method=%r, confirm_threshold=%r, erode_passes=%r)"
            % (self.match_method, self.match_threshold,
               self.confirm_method, self.confirm_threshold, self.erode_passes))


class Position(namedtuple('Position', 'x y')):
    """A point within the video frame.

    `x` and `y` are integer coordinates (measured in number of pixels) from the
    top left corner of the video frame.
    """
    pass


class MatchResult(object):
    """The result from `match`.

    :ivar float time: The time at which the video-frame was captured, in
        seconds since 1970-01-01T00:00Z. This timestamp can be compared with
        system time (``time.time()``).

    :ivar bool match: True if a match was found. This is the same as evaluating
        ``MatchResult`` as a bool. That is, ``if result:`` will behave the same
        as ``if result.match:``.

    :ivar Region region: Coordinates where the image was found (or of the
        nearest match, if no match was found).

    :ivar float first_pass_result: Value between 0 (poor) and 1.0 (excellent
        match) from the first pass of stb-tester's image matching algorithm
        (see `MatchParameters` for details).

    :ivar Frame frame: The video frame that was searched, as given to `match`.

    :ivar image: The reference image that was searched for, as given to `match`.
    """
    def __init__(
            self, time, match, region, first_pass_result, frame, image,
            _first_pass_matched=None):
        self.time = time
        self.match = match
        self.region = region
        self.first_pass_result = first_pass_result
        self.frame = frame
        self.image = image
        self._first_pass_matched = _first_pass_matched

    def __repr__(self):
        return (
            "MatchResult(time=%s, match=%r, region=%r, first_pass_result=%r, "
            "frame=%s, image=%s)" % (
                "None" if self.time is None else "%.3f" % self.time,
                self.match,
                self.region,
                self.first_pass_result,
                _frame_repr(self.frame),
                "<Custom Image>" if isinstance(self.image, numpy.ndarray)
                else repr(self.image)))

    def __nonzero__(self):
        return self.match

    @property
    def position(self):
        return Position(self.region.x, self.region.y)


def load_image(filename, flags=cv2.IMREAD_COLOR):
    """Find & read an image from disk.

    If given a relative filename, this will search in the directory of the
    Python file that called ``load_image``, then in the directory of that
    file's caller, etc. This allows you to use ``load_image`` in a helper
    function, and then call that helper function from a different Python file
    passing in a filename relative to the caller.

    Finally this will search in the current working directory. This allows
    loading an image that you had previously saved to disk during the same
    test run.

    This is the same lookup algorithm used by `stbt.match` and similar
    functions.

    :type filename: str or unicode
    :param filename: A relative or absolute filename.

    :param flags: Flags to pass to :ocv:pyfunc:`cv2.imread`.

    :returns: An image in OpenCV format (a `numpy.ndarray` of 8-bit values, 3
        channel BGR).
    :raises: `IOError` if the specified path doesn't exist or isn't a valid
        image file.

    Added in v28.
    """

    absolute_filename = find_user_file(filename)
    if not absolute_filename:
        raise IOError("No such file: %s" % filename)
    image = cv2.imread(absolute_filename, flags)
    if image is None:
        raise IOError("Failed to load image: %s" % absolute_filename)
    return image


class _IsScreenBlackResult(object):
    def __init__(self, black, frame):
        self.black = black
        self.frame = frame

    def __nonzero__(self):
        return self.black

    def __repr__(self):
        return ("_IsScreenBlackResult(black=%r, frame=%s)" % (
            self.black,
            _frame_repr(self.frame)))


def new_device_under_test_from_config(
        parsed_args=None, transformation_pipeline=None):
    """
    `parsed_args` if present should come from calling argparser().parse_args().
    """
    from _stbt.control import uri_to_control

    if parsed_args is None:
        args = argparser().parse_args([])
    else:
        args = parsed_args

    if args.source_pipeline is None:
        args.source_pipeline = get_config('global', 'source_pipeline')
    if args.sink_pipeline is None:
        args.sink_pipeline = get_config('global', 'sink_pipeline')
    if args.control is None:
        args.control = get_config('global', 'control')
    if args.save_video is None:
        args.save_video = False
    if args.restart_source is None:
        args.restart_source = get_config('global', 'restart_source', type_=bool)
    if transformation_pipeline is None:
        transformation_pipeline = get_config('global',
                                             'transformation_pipeline')
    source_teardown_eos = get_config('global', 'source_teardown_eos',
                                     type_=bool)
    use_old_threading_behaviour = get_config(
        'global', 'use_old_threading_behaviour', type_=bool)
    if use_old_threading_behaviour:
        warn(dedent("""\
            global.use_old_threading_behaviour is enabled.  This is intended as
            a stop-gap measure to allow upgrading to stb-tester v28. We
            recommend porting functions that depend on stbt.get_frame()
            returning consecutive frames on each call to use stbt.frames()
            instead.  This should make your functions usable from multiple
            threads.

            If porting to stbt.frames is not suitable please let us know on
            https://github.com/stb-tester/stb-tester/pull/449 otherwise this
            configuration option will be removed in a future release of
            stb-tester.
            """))

    display = [None]

    def raise_in_user_thread(exception):
        display[0].tell_user_thread(exception)
    mainloop = _mainloop()

    if not args.sink_pipeline and not args.save_video:
        sink_pipeline = NoSinkPipeline()
    else:
        sink_pipeline = SinkPipeline(  # pylint: disable=redefined-variable-type
            args.sink_pipeline, raise_in_user_thread, args.save_video)

    display[0] = Display(
        args.source_pipeline, sink_pipeline, args.restart_source,
        transformation_pipeline, source_teardown_eos)
    return DeviceUnderTest(
        display=display[0], control=uri_to_control(args.control, display[0]),
        sink_pipeline=sink_pipeline, mainloop=mainloop,
        use_old_threading_behaviour=use_old_threading_behaviour)


class DeviceUnderTest(object):
    def __init__(self, display=None, control=None, sink_pipeline=None,
                 mainloop=None, use_old_threading_behaviour=False, _time=None):
        if _time is None:
            import time as _time
        self._time_of_last_press = None
        self._display = display
        self._control = control
        self._sink_pipeline = sink_pipeline
        self._mainloop = mainloop
        self._time = _time

        self._use_old_threading_behaviour = use_old_threading_behaviour
        self._last_grabbed_frame_time = 0

    def __enter__(self):
        if self._display:
            self._mainloop.__enter__()
            self._sink_pipeline.__enter__()
            self._display.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if self._display:
            self._display.__exit__(exc_type, exc_value, tb)
            self._display = None
            self._sink_pipeline.__exit__(exc_type, exc_value, tb)
            self._sink_pipeline = None
            self._mainloop.__exit__(exc_type, exc_value, tb)
        self._control = None

    def press(self, key, interpress_delay_secs=None, hold_secs=None):
        if hold_secs is not None and hold_secs > 60:
            # You must ensure that lircd's --repeat-max is set high enough.
            raise ValueError("press: hold_secs must be less than 60 seconds")

        if hold_secs is None:
            with self._interpress_delay(interpress_delay_secs):
                self._control.press(key)
            self.draw_text(key, duration_secs=3)

        else:
            try:
                self._control.keydown(key)
                self.draw_text("Holding %s" % key,
                               duration_secs=min(3, hold_secs))
                self._time.sleep(hold_secs)
            finally:
                self._control.keyup(key)
                self.draw_text("Released %s" % key, duration_secs=3)

    @contextmanager
    def pressing(self, key, interpress_delay_secs=None):
        with self._interpress_delay(interpress_delay_secs):
            try:
                self._control.keydown(key)
                self.draw_text("Holding %s" % key, duration_secs=3)
                yield
            finally:
                original_exc_type, exc_value, exc_traceback = sys.exc_info()
                try:
                    self._control.keyup(key)
                    self.draw_text("Released %s" % key, duration_secs=3)
                except Exception:  # pylint:disable=broad-except
                    # Raise an exception if we fail to release the key, but
                    # not if it would mask an exception from the test script.
                    if original_exc_type is None:
                        raise
                if original_exc_type is not None:
                    raise original_exc_type, exc_value, exc_traceback  # pylint:disable=raising-bad-type

    @contextmanager
    def _interpress_delay(self, interpress_delay_secs):
        if interpress_delay_secs is None:
            interpress_delay_secs = get_config(
                "press", "interpress_delay_secs", type_=float)
        if self._time_of_last_press is not None:
            # `sleep` is inside a `while` loop because the actual suspension
            # time of `sleep` may be less than that requested.
            while True:
                seconds_to_wait = (
                    self._time_of_last_press - datetime.datetime.now() +
                    datetime.timedelta(seconds=interpress_delay_secs)
                ).total_seconds()
                if seconds_to_wait > 0:
                    self._time.sleep(seconds_to_wait)
                else:
                    break

        try:
            yield
        finally:
            self._time_of_last_press = datetime.datetime.now()

    def draw_text(self, text, duration_secs=3):
        self._sink_pipeline.draw(text, duration_secs)

    def match(self, image, frame=None, match_parameters=None,
              region=Region.ALL):
        result = next(self._match_all(image, frame, match_parameters, region))
        if result.match:
            debug("Match found: %s" % str(result))
        else:
            debug("No match found. Closest match: %s" % str(result))
        return result

    def match_all(self, image, frame=None, match_parameters=None,
                  region=Region.ALL):
        any_matches = False
        for result in self._match_all(image, frame, match_parameters, region):
            if result.match:
                debug("Match found: %s" % str(result))
                any_matches = True
                yield result
            else:
                if not any_matches:
                    debug("No match found. Closest match: %s" % str(result))
                break

    def _match_all(self, image, frame, match_parameters, region):
        """
        Generator that yields a sequence of zero or more truthy MatchResults,
        followed by a falsey MatchResult.
        """
        if match_parameters is None:
            match_parameters = MatchParameters()

        template = _load_image(image)

        if frame is None:
            frame = self.get_frame()

        imglog = logging.ImageLogger(
            "match", match_parameters=match_parameters,
            template_name=template.friendly_name)

        region = Region.intersect(_image_region(frame), region)

        # pylint:disable=undefined-loop-variable
        try:
            for (matched, match_region, first_pass_matched,
                 first_pass_certainty) in _find_matches(
                    crop(frame, region), template.image,
                    match_parameters, imglog):

                match_region = Region.from_extents(*match_region) \
                                     .translate(region.x, region.y)
                result = MatchResult(
                    getattr(frame, "time", None), matched, match_region,
                    first_pass_certainty, frame,
                    (template.relative_filename or template.image),
                    first_pass_matched)
                imglog.append(matches=result)
                draw_on(frame, result, label="match(%r)" %
                        os.path.basename(template.friendly_name))
                yield result

        finally:
            try:
                _log_match_image_debug(imglog)
            except Exception:  # pylint:disable=broad-except
                pass

    def detect_match(self, image, timeout_secs=10, match_parameters=None,
                     region=Region.ALL):
        template = _load_image(image)

        debug("Searching for " + template.friendly_name)

        for frame in self.frames(timeout_secs):
            result = self.match(
                template, frame=frame, match_parameters=match_parameters,
                region=region)
            draw_on(frame, result, label="match(%r)" %
                    os.path.basename(template.friendly_name))
            yield result

    def wait_for_match(self, image, timeout_secs=10, consecutive_matches=1,
                       match_parameters=None, region=Region.ALL):

        if match_parameters is None:
            match_parameters = MatchParameters()

        match_count = 0
        last_pos = Position(0, 0)
        image = _load_image(image)
        for res in self.detect_match(
                image, timeout_secs, match_parameters=match_parameters,
                region=region):
            if res.match and (match_count == 0 or res.position == last_pos):
                match_count += 1
            else:
                match_count = 0
            last_pos = res.position
            if match_count == consecutive_matches:
                debug("Matched " + image.friendly_name)
                return res

        raise MatchTimeout(res.frame, image.friendly_name, timeout_secs)  # pylint: disable=W0631,C0301

    def press_until_match(
            self,
            key,
            image,
            interval_secs=None,
            max_presses=None,
            match_parameters=None,
            region=Region.ALL):

        if interval_secs is None:
            # Should this be float?
            interval_secs = get_config(
                "press_until_match", "interval_secs", type_=int)
        if max_presses is None:
            max_presses = get_config(
                "press_until_match", "max_presses", type_=int)

        if match_parameters is None:
            match_parameters = MatchParameters()

        i = 0

        while True:
            try:
                return self.wait_for_match(image, timeout_secs=interval_secs,
                                           match_parameters=match_parameters,
                                           region=region)
            except MatchTimeout:
                if i < max_presses:
                    self.press(key)
                    i += 1
                else:
                    raise

    def frames(self, timeout_secs=None):
        if timeout_secs is not None:
            end_time = self._time.time() + timeout_secs
        timestamp = None
        first = True

        while True:
            if self._use_old_threading_behaviour:
                timestamp = self._last_grabbed_frame_time

            ddebug("user thread: Getting sample at %s" % self._time.time())
            frame = self._display.get_frame(
                max(10, timeout_secs), since=timestamp)
            ddebug("user thread: Got sample at %s" % self._time.time())
            timestamp = frame.time

            if self._use_old_threading_behaviour:
                self._last_grabbed_frame_time = timestamp

            if not first and timeout_secs is not None and timestamp > end_time:
                debug("timed out: %.3f > %.3f" % (timestamp, end_time))
                return

            yield frame
            first = False

    def get_frame(self):
        if self._use_old_threading_behaviour:
            frame = self._display.get_frame(
                since=self._last_grabbed_frame_time).copy()
            self._last_grabbed_frame_time = frame.time
            return frame
        else:
            return self._display.get_frame()

    def is_screen_black(self, frame=None, mask=None, threshold=None,
                        region=Region.ALL):
        if threshold is None:
            threshold = get_config('is_screen_black', 'threshold', type_=int)

        if frame is None:
            frame = self.get_frame()

        if mask is None:
            mask = _ImageFromUser(None, None, None)
        else:
            mask = _load_image(mask, cv2.IMREAD_GRAYSCALE)

        _region = Region.intersect(_image_region(frame), region)
        greyframe = cv2.cvtColor(crop(frame, _region), cv2.COLOR_BGR2GRAY)
        if mask.image is not None:
            cv2.bitwise_and(greyframe, mask.image, dst=greyframe)
        maxVal = greyframe.max()

        if logging.get_debug_level() > 1:
            imglog = logging.ImageLogger("is_screen_black")
            imglog.imwrite("source", frame)
            if mask.image is not None:
                imglog.imwrite('mask', mask.image)
            _, thresholded = cv2.threshold(greyframe, threshold, 255,
                                           cv2.THRESH_BINARY)
            imglog.imwrite('non-black-regions-after-masking', thresholded)

        result = _IsScreenBlackResult(bool(maxVal <= threshold), frame)
        debug("is_screen_black: {found} black screen using mask={mask}, "
              "threshold={threshold}, region={region}: "
              "{result}, maximum_intensity={maxVal}".format(
                  found="Found" if result.black else "Didn't find",
                  mask=mask.friendly_name,
                  threshold=threshold,
                  region=region,
                  result=result,
                  maxVal=maxVal))
        return result


# Utility functions
# ===========================================================================


def save_frame(image, filename):
    """Saves an OpenCV image to the specified file.

    Takes an image obtained from `get_frame` or from the `screenshot`
    property of `MatchTimeout` or `MotionTimeout`.
    """
    cv2.imwrite(filename, image)


def wait_until(callable_, timeout_secs=10, interval_secs=0, predicate=None,
               stable_secs=0):
    """Wait until a condition becomes true, or until a timeout.

    Calls ``callable_`` repeatedly (with a delay of ``interval_secs`` seconds
    between successive calls) until it succeeds (that is, it returns a
    `truthy`_ value) or until ``timeout_secs`` seconds have passed.

    .. _truthy: https://docs.python.org/2/library/stdtypes.html#truth-value-testing

    :param callable_: any Python callable (such as a function or a lambda
        expression) with no arguments.

    :type timeout_secs: int or float, in seconds
    :param timeout_secs: After this timeout elapses, ``wait_until`` will return
        the last value that ``callable_`` returned, even if it's falsey.

    :type interval_secs: int or float, in seconds
    :param interval_secs: Delay between successive invocations of ``callable_``.

    :param predicate: A function that takes a single value. It will be given
        the return value from ``callable_``. The return value of *this* function
        will then be used to determine truthiness. If the predicate test
        succeeds, ``wait_until`` will still return the original value from
        ``callable_``, not the predicate value.

    :type stable_secs: int or float, in seconds
    :param stable_secs: Wait for ``callable_``'s return value to remain the same
        (as determined by ``==``) for this duration before returning. If
        ``predicate`` is also given, the values returned from ``predicate``
        will be compared.

    :returns: The return value from ``callable_`` (which will be truthy if it
        succeeded, or falsey if ``wait_until`` timed out). If the value was
        truthy when the timeout was reached but it failed the ``predicate`` or
        ``stable_secs`` conditions (if any) then ``wait_until`` returns
        ``None``.

    After you send a remote-control signal to the device-under-test it usually
    takes a few frames to react, so a test script like this would probably
    fail::

        press("KEY_EPG")
        assert match("guide.png")

    Instead, use this::

        press("KEY_EPG")
        assert wait_until(lambda: match("guide.png"))

    Note that instead of the above ``assert wait_until(...)`` you could use
    ``wait_for_match("guide.png")``. ``wait_until`` is a generic solution that
    also works with stbt's other functions, like `match_text` and
    `is_screen_black`.

    ``wait_until`` allows composing more complex conditions, such as::

        # Wait until something disappears:
        assert wait_until(lambda: not match("xyz.png"))

        # Assert that something doesn't appear within 10 seconds:
        assert not wait_until(lambda: match("xyz.png"))

        # Assert that two images are present at the same time:
        assert wait_until(lambda: match("a.png") and match("b.png"))

        # Wait but don't raise an exception:
        if not wait_until(lambda: match("xyz.png")):
            do_something_else()

        # Wait for a menu selection to change. Here ``Menu`` is a `FrameObject`
        # with a property called `selection` that returns a string with the
        # name of the currently-selected menu item:
        # The return value (``menu``) is an instance of ``Menu``.
        menu = wait_until(Menu, predicate=lambda x: x.selection == "Home")

        # Wait for a match to stabilise position, returning the first stable
        # match. Used in performance measurements, for example to wait for a
        # selection highlight to finish moving:
        press("KEY_DOWN")
        start_time = time.time()
        match_result = wait_until(lambda: stbt.match("selection.png"),
                                  predicate=lambda x: x and x.region,
                                  stable_secs=2)
        assert match_result
        end_time = match_result.time  # this is the first stable frame
        print "Transition took %s seconds" % (end_time - start_time)

    Added in v28: The ``predicate`` and ``stable_secs`` parameters.
    """
    import time

    if predicate is None:
        predicate = lambda x: x
    stable_value = None
    stable_predicate_value = None
    expiry_time = time.time() + timeout_secs

    while True:
        t = time.time()
        value = callable_()
        predicate_value = predicate(value)

        if stable_secs:
            if predicate_value != stable_predicate_value:
                stable_since = t
                stable_value = value
                stable_predicate_value = predicate_value
            if predicate_value and t - stable_since >= stable_secs:
                return stable_value
        else:
            if predicate_value:
                return value

        if t >= expiry_time:
            debug("wait_until timed out: %s" % _callable_description(callable_))
            if not value:
                return value  # it's falsey
            else:
                return None  # must have failed stable_secs or predicate checks

        time.sleep(interval_secs)


def _callable_description(callable_):
    """Helper to provide nicer debug output when `wait_until` fails.

    >>> _callable_description(wait_until)
    'wait_until'
    >>> _callable_description(
    ...     lambda: stbt.press("OK"))
    '    lambda: stbt.press("OK"))\\n'
    >>> _callable_description(functools.partial(int, base=2))
    'int'
    >>> _callable_description(functools.partial(functools.partial(int, base=2),
    ...                                         x='10'))
    'int'
    >>> class T(object):
    ...     def __call__(self): return True;
    >>> _callable_description(T())
    '<_stbt.core.T object at 0x...>'
    """
    if hasattr(callable_, "__name__"):
        name = callable_.__name__
        if name == "<lambda>":
            try:
                name = inspect.getsource(callable_)
            except IOError:
                pass
        return name
    elif isinstance(callable_, functools.partial):
        return _callable_description(callable_.func)
    else:
        return repr(callable_)


@contextmanager
def as_precondition(message):
    """Context manager that replaces test failures with test errors.

    Stb-tester's reports show test failures (that is, `UITestFailure` or
    `AssertionError` exceptions) as red results, and test errors (that is,
    unhandled exceptions of any other type) as yellow results. Note that
    `wait_for_match`, `wait_for_motion`, and similar functions raise a
    `UITestFailure` when they detect a failure. By running such functions
    inside an `as_precondition` context, any `UITestFailure` or
    `AssertionError` exceptions they raise will be caught, and a
    `PreconditionError` will be raised instead.

    When running a single testcase hundreds or thousands of times to reproduce
    an intermittent defect, it is helpful to mark unrelated failures as test
    errors (yellow) rather than test failures (red), so that you can focus on
    diagnosing the failures that are most likely to be the particular defect
    you are looking for. For more details see `Test failures vs. errors
    <http://stb-tester.com/preconditions>`__.

    :param str message:
        A description of the precondition. Word this positively: "Channels
        tuned", not "Failed to tune channels".

    :raises:
        `PreconditionError` if the wrapped code block raises a `UITestFailure`
        or `AssertionError`.

    Example::

        def test_that_the_on_screen_id_is_shown_after_booting():
            channel = 100

            with stbt.as_precondition("Tuned to channel %s" % channel):
                mainmenu.close_any_open_menu()
                channels.goto_channel(channel)
                power.cold_reboot()
                assert channels.is_on_channel(channel)

            stbt.wait_for_match("on-screen-id.png")

    """
    try:
        yield
    except (UITestFailure, AssertionError) as e:
        debug("stbt.as_precondition caught a %s exception and will "
              "re-raise it as PreconditionError.\nOriginal exception was:\n%s"
              % (type(e).__name__, traceback.format_exc(e)))
        exc = PreconditionError(message, e)
        if hasattr(e, 'screenshot'):
            exc.screenshot = e.screenshot  # pylint: disable=attribute-defined-outside-init,no-member
        raise exc


class NoVideo(Exception):
    """No video available from the source pipeline."""
    pass


class MatchTimeout(UITestFailure):
    """Exception raised by `wait_for_match`.

    :ivar Frame screenshot: The last video frame that `wait_for_match` checked
        before timing out.

    :ivar str expected: Filename of the image that was being searched for.

    :vartype timeout_secs: int or float
    :ivar timeout_secs: Number of seconds that the image was searched for.
    """
    def __init__(self, screenshot, expected, timeout_secs):
        super(MatchTimeout, self).__init__()
        self.screenshot = screenshot
        self.expected = expected
        self.timeout_secs = timeout_secs

    def __str__(self):
        return "Didn't find match for '%s' within %g seconds." % (
            self.expected, self.timeout_secs)


class PreconditionError(UITestError):
    """Exception raised by `as_precondition`."""
    def __init__(self, message, original_exception):
        super(PreconditionError, self).__init__()
        self.message = message
        self.original_exception = original_exception

    def __str__(self):
        return (
            "Didn't meet precondition '%s' (original exception was: %s)"
            % (self.message, self.original_exception))


# stbt-run initialisation and convenience functions
# (you will need these if writing your own version of stbt-run)
# ===========================================================================

def argparser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--control',
        default=get_config('global', 'control'),
        help='The remote control to control the stb (default: %(default)s)')
    parser.add_argument(
        '--source-pipeline',
        default=get_config('global', 'source_pipeline'),
        help='A gstreamer pipeline to use for A/V input (default: '
             '%(default)s)')
    parser.add_argument(
        '--sink-pipeline',
        default=get_config('global', 'sink_pipeline'),
        help='A gstreamer pipeline to use for video output '
             '(default: %(default)s)')
    parser.add_argument(
        '--restart-source', action='store_true',
        default=get_config('global', 'restart_source', type_=bool),
        help='Restart the GStreamer source pipeline when video loss is '
             'detected')
    parser.add_argument(
        '--save-video', help='Record video to the specified file',
        metavar='FILE', default=get_config('run', 'save_video'))

    logging.argparser_add_verbose_argument(parser)

    return parser


def _memoize_property_fn(fn):
    @functools.wraps(fn)
    def inner(self):
        # pylint: disable=protected-access
        if fn not in self._FrameObject__frame_object_cache:
            self._FrameObject__frame_object_cache[fn] = fn(self)
        return self._FrameObject__frame_object_cache[fn]
    return inner


def _mark_in_is_visible(fn):
    @functools.wraps(fn)
    def inner(self):
        # pylint: disable=protected-access
        try:
            self._FrameObject__local.in_is_visible += 1
        except AttributeError:
            self._FrameObject__local.in_is_visible = 1
        try:
            return bool(fn(self))
        finally:
            self._FrameObject__local.in_is_visible -= 1
    return inner


def _noneify_property_fn(fn):
    @functools.wraps(fn)
    def inner(self):
        # pylint: disable=protected-access
        if (getattr(self._FrameObject__local, "in_is_visible", 0) or
                self.is_visible):
            return fn(self)
        else:
            return None
    return inner


class _FrameObjectMeta(type):
    def __new__(mcs, name, parents, dct):
        for k, v in dct.iteritems():
            if isinstance(v, property):
                # Properties must not have setters
                if v.fset is not None:
                    raise Exception(
                        "FrameObjects must be immutable but this property has "
                        "a setter")
                f = v.fget
                # The value of any property is cached after the first use
                f = _memoize_property_fn(f)
                # Public properties return `None` if the FrameObject isn't
                # visible.
                if k == 'is_visible':
                    f = _mark_in_is_visible(f)
                elif not k.startswith('_'):
                    f = _noneify_property_fn(f)
                dct[k] = property(f)

        if 'AUTO_SELFTEST_EXPRESSIONS' not in dct:
            dct['AUTO_SELFTEST_EXPRESSIONS'] = ['%s(frame={frame})' % name]

        return super(_FrameObjectMeta, mcs).__new__(mcs, name, parents, dct)

    def __init__(cls, name, parents, dct):
        property_names = sorted([
            p for p in dir(cls)
            if isinstance(getattr(cls, p), property)])
        assert 'is_visible' in property_names
        cls._FrameObject__attrs = ["is_visible"] + sorted(
            x for x in property_names
            if x != "is_visible" and not x.startswith('_'))
        super(_FrameObjectMeta, cls).__init__(name, parents, dct)


class FrameObject(object):
    __metaclass__ = _FrameObjectMeta

    def __init__(self, frame):
        if frame is None:
            raise ValueError("FrameObject: frame must not be None")
        self.__frame_object_cache = {}
        self.__local = threading.local()
        self._frame = frame

    def __repr__(self):
        args = ", ".join(("%s=%r" % x) for x in self._iter_attrs())
        return "%s(%s)" % (self.__class__.__name__, args)

    def _iter_attrs(self):
        if self:
            # pylint: disable=protected-access,no-member
            for x in self.__class__.__attrs:
                yield x, getattr(self, x)
        else:
            yield "is_visible", False

    def __nonzero__(self):
        return bool(self.is_visible)

    def __cmp__(self, other):
        # pylint: disable=protected-access
        from itertools import izip_longest
        if isinstance(other, self.__class__):
            for s, o in izip_longest(self._iter_attrs(), other._iter_attrs()):
                v = cmp(s[1], o[1])
                if v != 0:
                    return v
            return 0
        else:
            return NotImplemented

    def __hash__(self):
        return hash(tuple(v for _, v in self._iter_attrs()))

    @property
    def is_visible(self):
        raise NotImplementedError(
            "Objects deriving from FrameObject must define an is_visible "
            "property")


# Internal
# ===========================================================================


@contextmanager
def _mainloop():
    mainloop = GLib.MainLoop.new(context=None, is_running=False)

    thread = threading.Thread(target=mainloop.run)
    thread.daemon = True
    thread.start()

    try:
        yield
    finally:
        mainloop.quit()
        thread.join(10)
        debug("teardown: Exiting (GLib mainloop %s)" % (
              "is still alive!" if thread.isAlive() else "ok"))


class _Annotation(namedtuple("_Annotation", "time region label colour")):
    MATCHED = (32, 0, 255)  # Red
    NO_MATCH = (32, 255, 255)  # Yellow

    @staticmethod
    def from_result(result, label=""):
        colour = _Annotation.MATCHED if result else _Annotation.NO_MATCH
        return _Annotation(result.time, result.region, label, colour)

    def draw(self, img):
        if not self.region:
            return
        cv2.rectangle(
            img, (self.region.x, self.region.y),
            (self.region.right, self.region.bottom), self.colour,
            thickness=3)

        # Slightly above the match annotation
        label_loc = (self.region.x, self.region.y - 10)
        _draw_text(img, self.label, label_loc, (255, 255, 255), font_scale=0.5)


class _TextAnnotation(namedtuple("_TextAnnotation", "time text duration")):
    @property
    def end_time(self):
        return self.time + self.duration


class SinkPipeline(object):
    def __init__(self, user_sink_pipeline, raise_in_user_thread, save_video=""):
        import time as _time

        self.annotations_lock = threading.Lock()
        self.text_annotations = []
        self.annotations = []
        self._raise_in_user_thread = raise_in_user_thread
        self.received_eos = threading.Event()
        self._frames = deque(maxlen=35)
        self._time = _time
        self._sample_count = 0

        sink_pipeline_description = (
            "appsrc name=appsrc format=time is-live=true "
            "caps=video/x-raw,format=(string)BGR ")

        if save_video and user_sink_pipeline:
            sink_pipeline_description += "! tee name=t "
            src = "t. ! queue leaky=downstream"
        else:
            src = "appsrc."

        if save_video:
            if not save_video.endswith(".webm"):
                save_video += ".webm"
            debug("Saving video to '%s'" % save_video)
            sink_pipeline_description += (
                "{src} ! videoconvert ! "
                "vp8enc cpu-used=6 min_quantizer=32 max_quantizer=32 ! "
                "webmmux ! filesink location={save_video} ").format(
                src=src, save_video=save_video)

        if user_sink_pipeline:
            sink_pipeline_description += (
                "{src} ! videoconvert ! {user_sink_pipeline}").format(
                src=src, user_sink_pipeline=user_sink_pipeline)

        self.sink_pipeline = Gst.parse_launch(sink_pipeline_description)
        sink_bus = self.sink_pipeline.get_bus()
        sink_bus.connect("message::error", self._on_error)
        sink_bus.connect("message::warning", self._on_warning)
        sink_bus.connect("message::eos", self._on_eos_from_sink_pipeline)
        sink_bus.add_signal_watch()
        self.appsrc = self.sink_pipeline.get_by_name("appsrc")

        debug("sink pipeline: %s" % sink_pipeline_description)

    def _on_eos_from_sink_pipeline(self, _bus, _message):
        debug("Got EOS from sink pipeline")
        self.received_eos.set()

    def _on_warning(self, _bus, message):
        assert message.type == Gst.MessageType.WARNING
        Gst.debug_bin_to_dot_file_with_ts(
            self.sink_pipeline, Gst.DebugGraphDetails.ALL, "WARNING")
        err, dbg = message.parse_warning()
        warn("Warning: %s: %s\n%s\n" % (err, err.message, dbg))

    def _on_error(self, _bus, message):
        assert message.type == Gst.MessageType.ERROR
        if self.sink_pipeline is not None:
            Gst.debug_bin_to_dot_file_with_ts(
                self.sink_pipeline, Gst.DebugGraphDetails.ALL, "ERROR")
        err, dbg = message.parse_error()
        self._raise_in_user_thread(
            RuntimeError("%s: %s\n%s\n" % (err, err.message, dbg)))

    def __enter__(self):
        self.received_eos.clear()
        self.sink_pipeline.set_state(Gst.State.PLAYING)

    def __exit__(self, _1, _2, _3):
        # Drain the frame queue
        while self._frames:
            self._push_sample(self._frames.pop())

        if self._sample_count > 0:
            debug("teardown: Sending eos on sink pipeline")
            if self.appsrc.emit("end-of-stream") == Gst.FlowReturn.OK:
                self.sink_pipeline.send_event(Gst.Event.new_eos())
                if not self.received_eos.wait(10):
                    debug("Timeout waiting for sink EOS")
            else:
                debug("Sending EOS to sink pipeline failed")
        else:
            debug("SinkPipeline teardown: Not sending EOS, no samples sent")

        self.sink_pipeline.set_state(Gst.State.NULL)

        # Don't want to cause the Display object to hang around on our account,
        # we won't be raising any errors from now on anyway:
        self._raise_in_user_thread = None

    def on_sample(self, sample):
        """
        Called from `Display` for each frame.
        """
        # The test script can draw on the video, but this happens in a different
        # thread.  We don't know when they're finished drawing so we just give
        # them 0.5s instead.
        SINK_LATENCY_SECS = 0.5

        now = sample.time
        self._frames.appendleft(sample)

        while self._frames:
            oldest = self._frames.pop()
            if oldest.time > now - SINK_LATENCY_SECS:
                self._frames.append(oldest)
                break
            self._push_sample(oldest)

    def _push_sample(self, sample):
        # Calculate whether we need to draw any annotations on the output video.
        now = sample.time
        annotations = []
        with self.annotations_lock:
            # Remove expired annotations
            self.text_annotations = [x for x in self.text_annotations
                                     if now < x.end_time]
            current_texts = [x for x in self.text_annotations if x.time <= now]
            for annotation in list(self.annotations):
                if annotation.time == now:
                    annotations.append(annotation)
                if now >= annotation.time:
                    self.annotations.remove(annotation)

        sample = gst_sample_make_writable(sample)
        img = array_from_sample(sample, readwrite=True)
        # Text:
        _draw_text(
            img, datetime.datetime.now().strftime("%H:%M:%S.%f")[:-4],
            (10, 30), (255, 255, 255))
        for i, x in enumerate(reversed(current_texts)):
            origin = (10, (i + 2) * 30)
            age = float(now - x.time) / 3
            color = (int(255 * max([1 - age, 0.5])),) * 3
            _draw_text(img, x.text, origin, color)

        # Regions:
        for annotation in annotations:
            annotation.draw(img)

        self.appsrc.props.caps = sample.get_caps()
        self.appsrc.emit("push-buffer", sample.get_buffer())
        self._sample_count += 1

    def draw(self, obj, duration_secs=None, label=""):
        with self.annotations_lock:
            if isinstance(obj, str) or isinstance(obj, unicode):
                start_time = self._time.time()
                text = (
                    datetime.datetime.fromtimestamp(start_time).strftime(
                        "%H:%M:%S.%f")[:-4] +
                    ' ' + obj)
                self.text_annotations.append(
                    _TextAnnotation(start_time, text, duration_secs))
            elif hasattr(obj, "region") and hasattr(obj, "time"):
                annotation = _Annotation.from_result(obj, label=label)
                if annotation.time:
                    self.annotations.append(annotation)
            else:
                raise TypeError(
                    "Can't draw object of type '%s'" % type(obj).__name__)


class NoSinkPipeline(object):
    """
    Used in place of a SinkPipeline when no video output is required.  Is a lot
    faster because it doesn't do anything.  It especially doesn't do any copying
    nor video encoding :).
    """
    def __enter__(self):
        pass

    def __exit__(self, _1, _2, _3):
        pass

    def on_sample(self, _sample):
        pass

    def draw(self, _obj, _duration_secs=None, label=""):
        pass


class Display(object):
    def __init__(self, user_source_pipeline, sink_pipeline,
                 restart_source=False, transformation_pipeline='identity',
                 source_teardown_eos=False):

        import time

        self._condition = threading.Condition()  # Protects last_frame
        self.last_frame = None
        self.last_used_frame = None
        self.source_pipeline = None
        self.init_time = time.time()
        self.underrun_timeout = None
        self.tearing_down = False
        self.restart_source_enabled = restart_source
        self.source_teardown_eos = source_teardown_eos

        appsink = (
            "appsink name=appsink max-buffers=1 drop=false sync=true "
            "emit-signals=true "
            "caps=video/x-raw,format=BGR")
        # Notes on the source pipeline:
        # * _stbt_raw_frames_queue is kept small to reduce the amount of slack
        #   (and thus the latency) of the pipeline.
        # * _stbt_user_data_queue before the decodebin is large.  We don't want
        #   to drop encoded packets as this will cause significant image
        #   artifacts in the decoded buffers.  We make the assumption that we
        #   have enough horse-power to decode the incoming stream and any delays
        #   will be transient otherwise it could start filling up causing
        #   increased latency.
        self.source_pipeline_description = " ! ".join([
            user_source_pipeline,
            'queue name=_stbt_user_data_queue max-size-buffers=0 '
            '    max-size-bytes=0 max-size-time=10000000000',
            "decodebin",
            'queue name=_stbt_raw_frames_queue max-size-buffers=2',
            'videoconvert',
            'video/x-raw,format=BGR',
            transformation_pipeline,
            appsink])
        self.create_source_pipeline()

        self._sink_pipeline = sink_pipeline

        debug("source pipeline: %s" % self.source_pipeline_description)

    def create_source_pipeline(self):
        self.source_pipeline = Gst.parse_launch(
            self.source_pipeline_description)
        source_bus = self.source_pipeline.get_bus()
        source_bus.connect("message::error", self.on_error)
        source_bus.connect("message::warning", self.on_warning)
        source_bus.connect("message::eos", self.on_eos_from_source_pipeline)
        source_bus.add_signal_watch()
        appsink = self.source_pipeline.get_by_name("appsink")
        appsink.connect("new-sample", self.on_new_sample)

        # A realtime clock gives timestamps compatible with time.time()
        self.source_pipeline.use_clock(
            Gst.SystemClock(clock_type=Gst.ClockType.REALTIME))

        if self.restart_source_enabled:
            # Handle loss of video (but without end-of-stream event) from the
            # Hauppauge HDPVR capture device.
            source_queue = self.source_pipeline.get_by_name(
                "_stbt_user_data_queue")
            source_queue.connect("underrun", self.on_underrun)
            source_queue.connect("running", self.on_running)

    def set_source_pipeline_playing(self):
        if (self.source_pipeline.set_state(Gst.State.PAUSED) ==
                Gst.StateChangeReturn.NO_PREROLL):
            # This is a live source, drop frames if we get behind
            self.source_pipeline.get_by_name('_stbt_raw_frames_queue') \
                .set_property('leaky', 'downstream')
            self.source_pipeline.get_by_name('appsink') \
                .set_property('sync', False)

        self.source_pipeline.set_state(Gst.State.PLAYING)

    def get_frame(self, timeout_secs=10, since=None):
        import time
        t = time.time()
        end_time = t + timeout_secs
        if since is None:
            # If you want to wait 10s for a frame you're probably not interested
            # in a frame from 10s ago.
            since = t - timeout_secs

        with self._condition:
            while True:
                if (isinstance(self.last_frame, Frame) and
                        self.last_frame.time > since):
                    self.last_used_frame = self.last_frame
                    return self.last_frame
                elif isinstance(self.last_frame, Exception):
                    raise RuntimeError(str(self.last_frame))
                t = time.time()
                if t > end_time:
                    break
                self._condition.wait(end_time - t)

        pipeline = self.source_pipeline
        if pipeline:
            Gst.debug_bin_to_dot_file_with_ts(
                pipeline, Gst.DebugGraphDetails.ALL, "NoVideo")
        raise NoVideo("No video")

    def on_new_sample(self, appsink):
        sample = appsink.emit("pull-sample")

        running_time = sample.get_segment().to_running_time(
            Gst.Format.TIME, sample.get_buffer().pts)
        sample.time = (
            float(appsink.base_time + running_time) / 1e9)

        if (sample.time > self.init_time + 31536000 or
                sample.time < self.init_time - 31536000):  # 1 year
            warn("Received frame with suspicious timestamp: %f. Check your "
                 "source-pipeline configuration." % sample.time)

        frame = array_from_sample(sample)
        frame.flags.writeable = False

        # See also: logging.draw_on
        frame._draw_sink = weakref.ref(self._sink_pipeline)  # pylint: disable=protected-access
        self.tell_user_thread(frame)
        self._sink_pipeline.on_sample(sample)
        return Gst.FlowReturn.OK

    def tell_user_thread(self, frame_or_exception):
        # `self.last_frame` is how we communicate from this thread (the GLib
        # main loop) to the main application thread running the user's script.
        # Note that only this thread writes to self.last_frame.

        if isinstance(frame_or_exception, Exception):
            ddebug("glib thread: reporting exception to user thread: %s" %
                   frame_or_exception)
        else:
            ddebug("glib thread: new sample (time=%s)." %
                   frame_or_exception.time)

        with self._condition:
            self.last_frame = frame_or_exception
            self._condition.notify_all()

    def on_error(self, _bus, message):
        assert message.type == Gst.MessageType.ERROR
        pipeline = self.source_pipeline
        if pipeline is not None:
            Gst.debug_bin_to_dot_file_with_ts(
                pipeline, Gst.DebugGraphDetails.ALL, "ERROR")
        err, dbg = message.parse_error()
        self.tell_user_thread(
            RuntimeError("%s: %s\n%s\n" % (err, err.message, dbg)))

    def on_warning(self, _bus, message):
        assert message.type == Gst.MessageType.WARNING
        Gst.debug_bin_to_dot_file_with_ts(
            self.source_pipeline, Gst.DebugGraphDetails.ALL, "WARNING")
        err, dbg = message.parse_warning()
        warn("Warning: %s: %s\n%s\n" % (err, err.message, dbg))

    def on_eos_from_source_pipeline(self, _bus, _message):
        if not self.tearing_down:
            warn("Got EOS from source pipeline")
            self.restart_source()

    def on_underrun(self, _element):
        if self.underrun_timeout:
            ddebug("underrun: I already saw a recent underrun; ignoring")
        else:
            ddebug("underrun: scheduling 'restart_source' in 2s")
            self.underrun_timeout = GObjectTimeout(2, self.restart_source)
            self.underrun_timeout.start()

    def on_running(self, _element):
        if self.underrun_timeout:
            ddebug("running: cancelling underrun timer")
            self.underrun_timeout.cancel()
            self.underrun_timeout = None
        else:
            ddebug("running: no outstanding underrun timers; ignoring")

    def restart_source(self, *_args):
        warn("Attempting to recover from video loss: "
             "Stopping source pipeline and waiting 5s...")
        self.source_pipeline.set_state(Gst.State.NULL)
        self.source_pipeline = None
        GObjectTimeout(5, self.start_source).start()
        return False  # stop the timeout from running again

    def start_source(self):
        if self.tearing_down:
            return False
        warn("Restarting source pipeline...")
        self.create_source_pipeline()
        self.set_source_pipeline_playing()
        warn("Restarted source pipeline")
        if self.restart_source_enabled:
            self.underrun_timeout.start()
        return False  # stop the timeout from running again

    @staticmethod
    def appsink_await_eos(appsink, timeout=None):
        done = threading.Event()

        def on_eos(_appsink):
            done.set()
            return True
        hid = appsink.connect('eos', on_eos)
        d = appsink.get_property('eos') or done.wait(timeout)
        appsink.disconnect(hid)
        return d

    def __enter__(self):
        self.set_source_pipeline_playing()

    def __exit__(self, _1, _2, _3):
        self.tearing_down = True
        self.source_pipeline, source = None, self.source_pipeline
        if source:
            if self.source_teardown_eos:
                debug("teardown: Sending eos on source pipeline")
                for elem in gst_iterate(source.iterate_sources()):
                    elem.send_event(Gst.Event.new_eos())
                if not self.appsink_await_eos(
                        source.get_by_name('appsink'), timeout=10):
                    debug("Source pipeline did not teardown gracefully")
            source.set_state(Gst.State.NULL)
            source = None


def _draw_text(numpy_image, text, origin, color, font_scale=1.0):
    if not text:
        return

    (width, height), _ = cv2.getTextSize(
        text, fontFace=cv2.FONT_HERSHEY_DUPLEX, fontScale=font_scale,
        thickness=1)
    cv2.rectangle(
        numpy_image, (origin[0] - 2, origin[1] + 2),
        (origin[0] + width + 2, origin[1] - height - 2),
        thickness=cv2_compat.FILLED, color=(0, 0, 0))
    cv2.putText(
        numpy_image, text, origin, cv2.FONT_HERSHEY_DUPLEX,
        fontScale=font_scale, color=color, lineType=cv2_compat.LINE_AA)


class GObjectTimeout(object):
    """Responsible for setting a timeout in the GTK main loop."""
    def __init__(self, timeout_secs, handler, *args):
        self.timeout_secs = timeout_secs
        self.handler = handler
        self.args = args
        self.timeout_id = None

    def start(self):
        self.timeout_id = GObject.timeout_add(
            self.timeout_secs * 1000, self.handler, *self.args)

    def cancel(self):
        if self.timeout_id:
            GObject.source_remove(self.timeout_id)
        self.timeout_id = None


_BGR_CAPS = Gst.Caps.from_string('video/x-raw,format=BGR')


@imgproc_cache.memoize_iterator({"version": "25"})
def _find_matches(image, template, match_parameters, imglog):
    """Our image-matching algorithm.

    Runs 2 passes: `_find_candidate_matches` to locate potential matches, then
    `_confirm_match` to discard false positives from the first pass.

    Returns an iterator yielding zero or more `(True, position, certainty)`
    tuples for each location where `template` is found within `image`, followed
    by a single `(False, position, certainty)` tuple when there are no further
    matching locations.
    """

    if any(image.shape[x] < template.shape[x] for x in (0, 1)):
        raise ValueError("Source image must be larger than template image")
    if any(template.shape[x] < 1 for x in (0, 1)):
        raise ValueError("Template image must contain some data")
    if len(template.shape) != 3 or template.shape[2] != 3:
        raise ValueError("Template image must be 3 channel BGR")
    if template.dtype != numpy.uint8:
        raise ValueError("Template image must be 8-bits per channel")

    # pylint:disable=undefined-loop-variable
    for i, first_pass_matched, region, first_pass_certainty in \
            _find_candidate_matches(image, template, match_parameters,
                                    imglog):
        confirmed = (
            first_pass_matched and
            _confirm_match(image, region, template, match_parameters,
                           imwrite=lambda name, img: imglog.imwrite(
                               "match%d-%s" % (i, name), img)))  # pylint:disable=cell-var-from-loop

        yield (confirmed, list(region), first_pass_matched,
               first_pass_certainty)
        if not confirmed:
            break


def _find_candidate_matches(image, template, match_parameters, imglog):
    """First pass: Search for `template` in the entire `image`.

    This searches the entire image, so speed is more important than accuracy.
    False positives are ok; we apply a second pass later (`_confirm_match`) to
    weed out false positives.

    http://docs.opencv.org/modules/imgproc/doc/object_detection.html
    http://opencv-code.com/tutorials/fast-template-matching-with-image-pyramid
    """

    imglog.imwrite("source", image)
    imglog.imwrite("template", template)
    ddebug("Original image %s, template %s" % (image.shape, template.shape))

    method = {
        'sqdiff-normed': cv2.TM_SQDIFF_NORMED,
        'ccorr-normed': cv2.TM_CCORR_NORMED,
        'ccoeff-normed': cv2.TM_CCOEFF_NORMED,
    }[match_parameters.match_method]

    levels = get_config("match", "pyramid_levels", type_=int)
    if levels <= 0:
        raise ConfigurationError("'match.pyramid_levels' must be > 0")
    template_pyramid = _build_pyramid(template, levels)
    image_pyramid = _build_pyramid(image, len(template_pyramid))
    roi_mask = None  # Initial region of interest: The whole image.

    for level in reversed(range(len(template_pyramid))):
        if roi_mask is not None:
            if any(x < 3 for x in roi_mask.shape):
                roi_mask = None
            else:
                roi_mask = cv2.pyrUp(roi_mask)

        def imwrite(name, img):
            imglog.imwrite("level%d-%s" % (level, name), img)  # pylint:disable=cell-var-from-loop

        heatmap = _match_template(
            image_pyramid[level], template_pyramid[level], method,
            roi_mask, level, imwrite)

        # Relax the threshold slightly for scaled-down pyramid levels to
        # compensate for scaling artifacts.
        threshold = max(
            0,
            match_parameters.match_threshold - (0.2 if level > 0 else 0))

        matched, best_match_position, certainty = _find_best_match_position(
            heatmap, method, threshold, level)
        imglog.append(pyramid_levels=(
            matched, best_match_position, certainty, level))

        if not matched:
            break

        _, roi_mask = cv2.threshold(
            heatmap,
            ((1 - threshold) if method == cv2.TM_SQDIFF_NORMED else threshold),
            255,
            (cv2.THRESH_BINARY_INV if method == cv2.TM_SQDIFF_NORMED
             else cv2.THRESH_BINARY))
        roi_mask = roi_mask.astype(numpy.uint8)
        imwrite("source_matchtemplate_threshold", roi_mask)

    # pylint:disable=undefined-loop-variable
    region = Region(*_upsample(best_match_position, level),
                    width=template.shape[1], height=template.shape[0])

    for i in itertools.count():

        imglog.imwrite("match%d-heatmap" % i, heatmap)
        yield (i, matched, region, certainty)
        if not matched:
            return
        assert level == 0

        # Exclude any positions that would overlap the previous match, then
        # keep iterating until we don't find any more matches.
        exclude = region.extend(x=-(region.width - 1), y=-(region.height - 1))
        mask_value = (255 if match_parameters.match_method == 'sqdiff-normed'
                      else 0)
        cv2.rectangle(
            heatmap,
            # -1 because cv2.rectangle considers the bottom-right point to be
            # *inside* the rectangle.
            (exclude.x, exclude.y), (exclude.right - 1, exclude.bottom - 1),
            mask_value, cv2_compat.FILLED)

        matched, best_match_position, certainty = _find_best_match_position(
            heatmap, method, threshold, level)
        region = Region(*best_match_position,
                        width=template.shape[1], height=template.shape[0])


def _match_template(image, template, method, roi_mask, level, imwrite):

    ddebug("Level %d: image %s, template %s" % (
        level, image.shape, template.shape))

    matches_heatmap = (
        (numpy.ones if method == cv2.TM_SQDIFF_NORMED else numpy.zeros)(
            (image.shape[0] - template.shape[0] + 1,
             image.shape[1] - template.shape[1] + 1),
            dtype=numpy.float32))

    if roi_mask is None:
        rois = [  # Initial region of interest: The whole image.
            _Rect(0, 0, matches_heatmap.shape[1], matches_heatmap.shape[0])]
    else:
        rois = [_Rect(*x) for x in cv2_compat.find_contour_boxes(
            roi_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)]

    if logging.get_debug_level() > 1:
        source_with_rois = image.copy()
        for roi in rois:
            r = roi
            t = _Size(*template.shape[:2])
            s = _Size(*source_with_rois.shape[:2])
            cv2.rectangle(
                source_with_rois,
                (max(0, r.x), max(0, r.y)),
                (min(s.w - 1, r.x + r.w + t.w - 1),
                 min(s.h - 1, r.y + r.h + t.h - 1)),
                (0, 255, 255),
                thickness=1)
        imwrite("source_with_rois", source_with_rois)

    for roi in rois:
        r = roi.expand(_Size(*template.shape[:2])).shrink(_Size(1, 1))
        ddebug("Level %d: Searching in %s" % (level, roi))
        cv2.matchTemplate(
            image[r.to_slice()],
            template,
            method,
            matches_heatmap[roi.to_slice()])

    imwrite("source", image)
    imwrite("template", template)
    imwrite("source_matchtemplate", matches_heatmap)

    return matches_heatmap


def _find_best_match_position(matches_heatmap, method, threshold, level):
    min_value, max_value, min_location, max_location = cv2.minMaxLoc(
        matches_heatmap)
    if method == cv2.TM_SQDIFF_NORMED:
        certainty = (1 - min_value)
        best_match_position = Position(*min_location)
    elif method in (cv2.TM_CCORR_NORMED, cv2.TM_CCOEFF_NORMED):
        certainty = max_value
        best_match_position = Position(*max_location)
    else:
        raise ValueError("Invalid matchTemplate method '%s'" % method)

    matched = certainty >= threshold
    ddebug("Level %d: %s at %s with certainty %s" % (
        level, "Matched" if matched else "Didn't match",
        best_match_position, certainty))
    return (matched, best_match_position, certainty)


def _build_pyramid(image, levels):
    """A "pyramid" is [an image, the same image at 1/2 the size, at 1/4, ...]

    As a performance optimisation, image processing algorithms work on a
    "pyramid" by first identifying regions of interest (ROIs) in the smallest
    image; if results are positive, they proceed to the next larger image, etc.
    See http://docs.opencv.org/doc/tutorials/imgproc/pyramids/pyramids.html

    The original-sized image is called "level 0", the next smaller image "level
    1", and so on. This numbering corresponds to the array index of the
    "pyramid" array.
    """
    pyramid = [image]
    for _ in range(levels - 1):
        if any(x < 20 for x in pyramid[-1].shape[:2]):
            break
        pyramid.append(cv2.pyrDown(pyramid[-1]))
    return pyramid


def _upsample(position, levels):
    """Convert position coordinates by the given number of pyramid levels.

    There is a loss of precision (unless ``levels`` is 0, in which case this
    function is a no-op).
    """
    return Position(position.x * 2 ** levels, position.y * 2 ** levels)


# Order of parameters consistent with ``cv2.boundingRect``.
class _Rect(namedtuple("_Rect", "x y w h")):
    def expand(self, size):
        return _Rect(self.x, self.y, self.w + size.w, self.h + size.h)

    def shrink(self, size):
        return _Rect(self.x, self.y, self.w - size.w, self.h - size.h)

    def shift(self, position):
        return _Rect(self.x + position.x, self.y + position.y, self.w, self.h)

    def to_slice(self):
        """Return a 2-dimensional slice suitable for indexing a numpy array."""
        return (slice(self.y, self.y + self.h), slice(self.x, self.x + self.w))


# Order of parameters consistent with OpenCV's ``numpy.ndarray.shape``.
class _Size(namedtuple("_Size", "h w")):
    pass


def _confirm_match(image, region, template, match_parameters, imwrite):
    """Second pass: Confirm that `template` matches `image` at `region`.

    This only checks `template` at a single position within `image`, so we can
    afford to do more computationally-intensive checks than
    `_find_candidate_matches`.
    """

    if match_parameters.confirm_method == "none":
        return True

    # Set Region Of Interest to the "best match" location
    roi = image[region.y:region.bottom, region.x:region.right]
    image_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    imwrite("confirm-source_roi", roi)
    imwrite("confirm-source_roi_gray", image_gray)
    imwrite("confirm-template_gray", template_gray)

    if match_parameters.confirm_method == "normed-absdiff":
        cv2.normalize(image_gray, image_gray, 0, 255, cv2.NORM_MINMAX)
        cv2.normalize(template_gray, template_gray, 0, 255, cv2.NORM_MINMAX)
        imwrite("confirm-source_roi_gray_normalized", image_gray)
        imwrite("confirm-template_gray_normalized", template_gray)

    absdiff = cv2.absdiff(image_gray, template_gray)
    _, thresholded = cv2.threshold(
        absdiff, int(match_parameters.confirm_threshold * 255),
        255, cv2.THRESH_BINARY)
    eroded = cv2.erode(
        thresholded,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=match_parameters.erode_passes)
    imwrite("confirm-absdiff", absdiff)
    imwrite("confirm-absdiff_threshold", thresholded)
    imwrite("confirm-absdiff_threshold_erode", eroded)

    return cv2.countNonZero(eroded) == 0


def _log_match_image_debug(imglog):

    if not imglog.enabled:
        return

    for matched, position, _, level in imglog.data["pyramid_levels"]:
        template = imglog.images["level%d-template" % level]
        imglog.imwrite("level%d-source_with_match" % level,
                       imglog.images["level%d-source" % level],
                       Region(x=position.x, y=position.y,
                              width=template.shape[1],
                              height=template.shape[0]),
                       _Annotation.MATCHED if matched else _Annotation.NO_MATCH)

    for i, result in enumerate(imglog.data["matches"]):
        imglog.imwrite(
            "match%d-source_with_match" % i, imglog.images["source"],
            result.region, _Annotation.MATCHED if result._first_pass_matched  # pylint:disable=protected-access
            else _Annotation.NO_MATCH)

    try:
        import jinja2
    except ImportError:
        warn(
            "Not generating html view of the image-processing debug images,"
            " because python 'jinja2' module is not installed.")
        return

    template = jinja2.Template("""
        <!DOCTYPE html>
        <html lang='en'>
        <head>
        <link href="http://netdna.bootstrapcdn.com/twitter-bootstrap/2.3.2/css/bootstrap-combined.min.css" rel="stylesheet">
        <style>
            h5 { margin-top: 40px; }
            .table th { font-weight: normal; background-color: #eee; }
            img {
                vertical-align: middle; max-width: 150px; max-height: 36px;
                padding: 1px; border: 1px solid #ccc; }
            p { line-height: 40px; }
            .table td { vertical-align: middle; }
        </style>
        </head>
        <body>
        <div class="container">
        <h4>
            <i>{{template_name}}</i>
            {{"matched" if matched else "didn't match"}}
        </h4>

        <h5>First pass (find candidate matches):</h5>

        <p>Searching for <b>template</b> {{link("template")}}
            within <b>source</b> image {{link("source")}}

        <table class="table">
        <tr>
          <th>Pyramid level</th>
          <th>Match #</th>
          <th>Searching for <b>template</b></th>
          <th>within <b>source regions of interest</b></th>
          <th>
            OpenCV <b>matchTemplate heatmap</b>
            with method {{match_parameters.match_method}}
            ({{"darkest" if match_parameters.match_method ==
                    "sqdiff-normed" else "lightest"}}
            pixel indicates position of best match).
          </th>
          <th>
            matchTemplate heatmap <b>above match_threshold</b>
            of {{"%g"|format(match_parameters.match_threshold)}}
            (white pixels indicate positions above the threshold).
          </th>
          <th><b>Matched?<b></th>
          <th>Best match <b>position</b></th>
          <th>&nbsp;</th>
          <th><b>certainty</b></th>
        </tr>

        {% for matched, position, certainty, level in pyramid_levels %}
        <tr>
          <td><b>{{level}}</b></td>
          <td><b>{{"0" if level == 0 else ""}}</b></td>
          <td>{{link("template", level)}}</td>
          <td>{{link("source_with_rois", level)}}</td>
          <td>{{link("source_matchtemplate", level)}}</td>
          <td>
            {{link("source_matchtemplate_threshold", level) if matched else ""}}
          </td>
          <td>{{"Matched" if matched else "Didn't match"}}</td>
          <td>{{position if level > 0 else matches[0].region}}</td>
          <td>{{link("source_with_match", level)}}</td>
          <td>{{"%.4f"|format(certainty)}}</td>
        </tr>
        {% endfor %}

        {% for m in matches[1:] %}
        {# note that loop.index is 1-based #}
        <tr>
          <td>&nbsp;</td>
          <td><b>{{loop.index}}</b></td>
          <td>&nbsp;</td>
          <td>&nbsp;</td>
          <td>{{link("heatmap", match=loop.index)}}</td>
          <td></td>
          <td>{{"Matched" if m._first_pass_matched else "Didn't match"}}</td>
          <td>{{m.region}}</td>
          <td>{{link("source_with_match", match=loop.index)}}</td>
          <td>{{"%.4f"|format(m.first_pass_result)}}</td>
        </tr>
        {% endfor %}

        </table>

        {% if show_second_pass %}
          <h5>Second pass (confirmation):</h5>

          <p><b>Confirm method:</b> {{match_parameters.confirm_method}}</p>

          {% if match_parameters.confirm_method != "none" %}
            <table class="table">
            <tr>
              <th>Match #</th>
              <th>Comparing <b>template</b></th>
              <th>against <b>source image's region of interest</b></th>
              {% if match_parameters.confirm_method == "normed-absdiff" %}
                <th><b>Normalised template</b></th>
                <th><b>Normalised source</b></th>
              {% endif %}
              <th><b>Absolute differences</b></th>
              <th>
                Differences <b>above confirm_threshold</b>
                of {{"%.2f"|format(match_parameters.confirm_threshold)}}
              </th>
              <th>
                After <b>eroding</b>
                {{match_parameters.erode_passes}}
                {{"time" if match_parameters.erode_passes == 1
                  else "times"}};
                the template matches if no differences (white pixels) remain
              </th>
            </tr>

            {% for m in matches %}
              {% if m._first_pass_matched %}
                <tr>
                  <td><b>{{loop.index0}}</b></td>
                  <td>{{link("confirm-template_gray", match=0)}}</td>
                  <td>{{link("confirm-source_roi_gray", match=loop.index0)}}</td>
                  {% if match_parameters.confirm_method == "normed-absdiff" %}
                    <td>{{link("confirm-template_gray_normalized", match=loop.index0)}}</td>
                    <td>{{link("confirm-source_roi_gray_normalized", match=loop.index0)}}</td>
                  {% endif %}
                  <td>{{link("confirm-absdiff", match=loop.index0)}}</td>
                  <td>{{link("confirm-absdiff_threshold", match=loop.index0)}}</td>
                  <td>{{link("confirm-absdiff_threshold_erode", match=loop.index0)}}</td>
                </tr>
              {% endif %}
            {% endfor %}

            </table>
          {% endif %}
        {% endif %}

        <p>For further help please read
            <a href="http://stb-tester.com/match-parameters.html">stb-tester
            image matching parameters</a>.

        </div>
        </body>
        </html>
    """)

    def link(name, level=None, match=None):
        return ("<a href='{0}{1}{2}.png'><img src='{0}{1}{2}.png'></a>"
                .format("" if level is None else "level%d-" % level,
                        "" if match is None else "match%d-" % match,
                        name))

    with open(os.path.join(imglog.outdir, "index.html"), "w") as f:
        f.write(template.render(
            link=link,
            match_parameters=imglog.data["match_parameters"],
            matched=any(imglog.data["matches"]),
            matches=imglog.data["matches"],
            min=min,
            pyramid_levels=imglog.data["pyramid_levels"],
            show_second_pass=any(
                x._first_pass_matched for x in imglog.data["matches"]),  # pylint:disable=protected-access
            template_name=imglog.data["template_name"],
        ))
