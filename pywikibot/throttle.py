# -*- coding: utf-8 -*-
"""Mechanics to slow down wiki read and/or write rate."""
#
# (C) Pywikibot team, 2008-2020
#
# Distributed under the terms of the MIT license.
#
import math
import threading
import time

from collections import namedtuple
from contextlib import suppress
from typing import Optional

import pywikibot
from pywikibot import config
from pywikibot.tools import deprecated

_logger = 'wiki.throttle'

FORMAT_LINE = '{pid} {time} {site}\n'
ProcEntry = namedtuple('ProcEntry', ['pid', 'time', 'site'])

# global process identifier
#
# When the first Throttle is instantiated, it will set this variable to a
# positive integer, which will apply to all throttle objects created by this
# process.
pid = False


class Throttle:

    """Control rate of access to wiki server.

    Calling this object blocks the calling thread until at least 'delay'
    seconds have passed since the previous call.

    Each Site initiates one Throttle object (site.throttle) to control the
    rate of access.

    """

    def __init__(self, site, mindelay=None, maxdelay=None, writedelay=None,
                 multiplydelay=True):
        """Initializer."""
        self.lock = threading.RLock()
        self.lock_write = threading.RLock()
        self.lock_read = threading.RLock()
        self.mysite = str(site)
        self.ctrlfilename = config.datafilepath('throttle.ctrl')
        self.mindelay = mindelay or config.minthrottle
        self.maxdelay = maxdelay or config.maxthrottle
        self.writedelay = writedelay or config.put_throttle
        self.last_read = 0
        self.last_write = 0
        self.next_multiplicity = 1.0

        # Check logfile again after this many seconds:
        self.checkdelay = 300

        # Ignore processes that have not made a check in this many seconds:
        self.dropdelay = 600

        # Free the process id after this many seconds:
        self.releasepid = 1200

        self.retry_after = 0  # set by http.request
        self.delay = 0
        self.checktime = 0
        self.multiplydelay = multiplydelay
        if self.multiplydelay:
            self.checkMultiplicity()
        self.setDelays()

    @property
    @deprecated(since='20180423', future_warning=True)
    def lastwait(self):
        """DEPRECATED property."""
        return 0.0

    def _read_file(self, raise_exc=False):
        """Yield process entries from file."""
        try:
            with open(self.ctrlfilename, 'r') as f:
                lines = f.readlines()
        except IOError:
            if raise_exc and pid:
                raise
            return

        for line in lines:
            # parse line; format is "pid timestamp site"
            try:
                _pid, _time, _site = line.split(' ')
                proc_entry = ProcEntry(
                    pid=int(_pid),
                    time=int(float(_time)),
                    site=_site.rstrip()
                )
            except (IndexError, ValueError):
                # Sometimes the file gets corrupted ignore that line
                continue
            yield proc_entry

    def _write_file(self, processes):
        """Write process entries to file."""
        if not isinstance(processes, list):
            processes = list(processes)
        processes.sort(key=lambda p: (p.pid, p.site))

        with suppress(IOError), open(self.ctrlfilename, 'w') as f:
            for p in processes:
                f.write(FORMAT_LINE.format_map(p._asdict()))

    def checkMultiplicity(self):
        """Count running processes for site and set process_multiplicity."""
        global pid
        mysite = self.mysite
        pywikibot.debug('Checking multiplicity: pid = {pid}'.format(pid=pid),
                        _logger)
        with self.lock:
            processes = []
            my_pid = pid or 1  # start at 1 if global pid not yet set
            count = 1

            now = time.time()
            for proc in self._read_file(raise_exc=True):
                if now - proc.time > self.releasepid:
                    continue    # process has expired, drop from file
                if now - proc.time <= self.dropdelay \
                   and proc.site == mysite \
                   and proc.pid != pid:
                    count += 1
                if proc.site != self.mysite or proc.pid != pid:
                    processes.append(proc)
                if not pid and proc.pid >= my_pid:
                    my_pid = proc.pid + 1  # next unused process id

            if not pid:
                pid = my_pid
            self.checktime = time.time()
            processes.append(
                ProcEntry(pid=pid, time=self.checktime, site=mysite))

            self._write_file(processes)

            self.process_multiplicity = count
            pywikibot.log('Found {} {} processes running, including this one.'
                          .format(count, mysite))

    def setDelays(self, delay=None, writedelay=None, absolute=False):
        """Set the nominal delays in seconds. Defaults to config values."""
        with self.lock:
            delay = delay or self.mindelay
            writedelay = writedelay or config.put_throttle
            if absolute:
                self.maxdelay = delay
                self.mindelay = delay
            self.delay = delay
            self.writedelay = min(max(self.mindelay, writedelay),
                                  self.maxdelay)
            # Start the delay count now, not at the next check
            self.last_read = self.last_write = time.time()

    def getDelay(self, write=False):
        """Return the actual delay, accounting for multiple processes.

        This value is the maximum wait between reads/writes, not taking
        into account of how much time has elapsed since the last access.

        """
        if write:
            thisdelay = self.writedelay
        else:
            thisdelay = self.delay
        if not self.multiplydelay:
            return thisdelay

        # We're checking for multiple processes
        if time.time() > self.checktime + self.checkdelay:
            self.checkMultiplicity()
        if thisdelay < (self.mindelay * self.next_multiplicity):
            thisdelay = self.mindelay * self.next_multiplicity
        elif thisdelay > self.maxdelay:
            thisdelay = self.maxdelay
        thisdelay *= self.process_multiplicity
        return thisdelay

    def waittime(self, write=False):
        """Return waiting time in seconds.

        The result is for a query that would be made right now.
        """
        # Take the previous requestsize in account calculating the desired
        # delay this time
        thisdelay = self.getDelay(write=write)
        now = time.time()
        ago = now - (self.last_write if write else self.last_read)
        return max(0.0, thisdelay - ago)

    def drop(self):
        """Remove me from the list of running bot processes."""
        # drop all throttles with this process's pid, regardless of site
        self.checktime = 0

        now = time.time()
        processes = [p for p in self._read_file()
                     if now - p.time <= self.releasepid and p.pid != pid]

        self._write_file(processes)

    def wait(self, seconds):
        """Wait for seconds seconds.

        Announce the delay if it exceeds a preset limit.
        """
        if seconds <= 0:
            return

        message = 'Sleeping for {seconds:.1f} seconds, {now}' \
                  .format_map({
                      'seconds': seconds,
                      'now': time.strftime('%Y-%m-%d %H:%M:%S',
                                           time.localtime())})
        if seconds > config.noisysleep:
            pywikibot.output(message)
        else:
            pywikibot.log(message)

        time.sleep(seconds)

    def __call__(self, requestsize=1, write=False):
        """Block the calling program if the throttle time has not expired.

        Parameter requestsize is the number of Pages to be read/written;
        multiply delay time by an appropriate factor.

        Because this seizes the throttle lock, it will prevent any other
        thread from writing to the same site until the wait expires.

        """
        lock = self.lock_write if write else self.lock_read
        with lock:
            wait = self.waittime(write=write)
            # Calculate the multiplicity of the next delay based on how
            # big the request is that is being posted now.
            # We want to add "one delay" for each factor of two in the
            # size of the request. Getting 64 pages at once allows 6 times
            # the delay time for the server.
            self.next_multiplicity = math.log(1 + requestsize) / math.log(2.0)

            self.wait(wait)

            if write:
                self.last_write = time.time()
            else:
                self.last_read = time.time()

    def lag(self, lagtime: Optional[float] = None):
        """Seize the throttle lock due to server lag.

        Usually the self.retry-after value from response_header of the last
        request if available which will be used for wait time. Otherwise
        lagtime from api maxlag is used. If neither retry_after nor lagtime is
        set, fallback to config.retry_wait.

        If the lagtime is disproportionately high compared to retry-after
        value, the wait time will be increased.

        This method is used by api.request. It will prevent any thread from
        accessing this site.

        @param lagtime: The time to wait for the next request which is the
            last maxlag time from api warning. This is only used as a fallback
            if self.retry-after isn't set.
        """
        started = time.time()
        with self.lock:
            waittime = lagtime or config.retry_wait
            if self.retry_after:
                waittime = max(self.retry_after, waittime / 5)
            # wait not more than retry_max seconds
            delay = min(waittime, config.retry_max)
            # account for any time we waited while acquiring the lock
            wait = delay - (time.time() - started)
            self.wait(wait)
