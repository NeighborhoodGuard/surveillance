################################################################################
#
# Copyright (C) 2014-2018 Neighborhood Guard, Inc.  All rights reserved.
# Original author: Douglas Kerr
#
# This file is part of CommunityView.
#
# CommunityView is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CommunityView is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with CommunityView.  If not, see <http://www.gnu.org/licenses/>.
#
################################################################################

from localsettings import root # and lwebrootpath when there is one
import threading
import os.path
import csv
import datetime
from utils import dir2date, file2time, get_daydirs, get_images_in_dir, \
                    set_thread_prefix
import time
import logging
import platform
import re


# general exception for stats problems
class StatsError(Exception):
    pass

# One file per day per camera, plus one overall per-server file per day
#
#   per camera: YYYY-DD-MM_camerashortname.csv
#   per server: YYYY-DD-MM.csv
#
# All files are in lwebrootpath/perf.  Saved for same number of days as images.
# In-memory values are stored in a dictionary of datecam-minute lists for the
# per-camera values and date-minute lists for the server data.
# Key is filename minus extension, value points to list of 1440 (one per second)
# lists of stat values.
# 

# XXX hack for initial implementation on old CommunityView
lwebrootpath = root

statspath = os.path.join(lwebrootpath, "perf")

# the key for the dictionary are either datecams (for the per-day, per-camera
# data, or the string date (YYYY-MM-DD) for the per-day server data.
statdict = {}

# dictlock is only locked while insuring that a table is in memory and acquiring
# the lock to that particular table. It is not required to read or write a table
# in memory
dictlock = threading.RLock()

# true when the server was restarted during the current minute
restarted = False

# datecam and per-server tables statdict:
# top level is list: [RLock, table, changed]
LOCK = 0
TABLE = 1
CHANGED = 2

# datecam table column indicies
NCREATE     = 0 # number of uploaded images that were created during this minute
AVGUPLAT    = 1 # average upload latency for images created during this minute
NUPLOAD     = 2 # number of imgs processed that were uploaded during this minute
AVGPROCLAT  = 3 # avg processing latency for images uploaded during this minute
NPROC       = 4 # number of files processed during this minute
NUNPROC     = 5 # number of unprocessed files from today at this minute
NUNPROCPREV = 6 # number of unprocessed files from previous days at this minute

LENDCROW    = 7 # length of the datecam table row

# extra columns in per-server table
RESTARTED   = 7 # non-zero if server restarted during this minute
NERRORS     = 8 # future use: count of ERROR-level events during this minute

LENPSROW    = 9 # length of the per-server table row

# datecam CSV file column headers
DCCSVHEADERS = ("Time", "Images Created/Min", "Upload Latency", 
                "Images Uploaded/Min", "Processing Latency",
                "Images Processed/Min", "Today's Unprocessed Images", 
                "Previous Days' Unprocessed Images")

# extra column headers in per-server table
PSCSVHEADERS = DCCSVHEADERS + ("Restarted", "Errors")

# the number of rows in the datecam and server csv tables is equal to the number
# of minutes in a day
MINPERDAY = 1440



def datecam_to_fn(datecam):
    """Return the filename of the stats file associated with the datecam."""
    return datecam[0] + "_" + datecam[1] + ".csv"

def number(string):
    """Convert numeric string to a float or an int depending on its
    contents.  If string is an empty string or None, return None."""
    if not string:
        return None
    try:
        return int(string)
    except ValueError:
        return float(string)

def lock_datecam(datecam, changed=True):
    """Insure the stats table for the datecam or per-server (the summary table
    for the whole server for the day) is in memory, and return a tuple
    consisting of an acquired RLock for accessing the table (which must be
    released when the thread is done accessing/updating the table) and the
    table. If there is no existing table file, initialize all table values to
    None.  It is assumed that the table is being retrieved in order to make
    changes, so the changed flag defaults to True. 
    If the changed flag is set (default) the table will be marked to be
    written to the filesystem at the next one-minute tick.  If datecam 
    represents a per-server table, the "cam" part of the datecam is an
    empty string."""
    is_ps = datecam[1]==""
    dictlock.acquire() 
    if datecam not in statdict:
        # begin with an empty table
        trow = [None] * (LENPSROW if is_ps else LENDCROW)
        table = [None] * MINPERDAY  # initialization optimization
        table = [list(trow) for _ in range(MINPERDAY)]
        statdict[datecam] = [threading.RLock(), table,  changed]
        
        fp = os.path.join(statspath, datecam_to_fn(datecam))
        if os.path.isfile(fp):
            with open(fp, "rb") as csvfile:
                hh = csv.Sniffer().has_header(csvfile.read(1024))
                csvfile.seek(0)
                if not hh:
                    logging.warn("%s: no header row" % fp)
                reader = csv.reader(csvfile, delimiter=',', quotechar='"')
                rindex = 0
                for csvrow in reader:
                    if hh:  # skip the header row
                        hh = False
                        continue
                    if len(csvrow) != (LENPSROW if is_ps else LENDCROW) + 1:
                        raise StatsError("%s: line %d: wrong number of fields" \
                                % (fp, rindex+1))
                    csvrow = csvrow[1:]     # remove the time field
                    table[rindex] = [number(s) for s in csvrow]
                    rindex += 1
            if rindex != MINPERDAY:
                raise StatsError("%s: wrong number of data rows: %d" \
                                 % (fp, rindex))
    statdict[datecam][LOCK].acquire()
    statdict[datecam][CHANGED] = changed
    dictlock.release()
    return (statdict[datecam][LOCK], statdict[datecam][TABLE])

def zeroback(table, rowindex, colindex):
    """Starting with row rowindex-1 and working backward, replace all None
    values in the specified column of the table with integer zero until a
    non-None value is encountered."""
    rowindex -= 1
    while rowindex >= 0 and table[rowindex][colindex] is None:
        table[rowindex][colindex] = 0
        rowindex -= 1

def proc_stats(imagepath):
    """Called by image processing code to record image processing statistics for
    the given image file."""
    (p, filename) = os.path.split(imagepath)
    (p, cam) = os.path.split(p)
    (_, date) = os.path.split(p)
    createdatecam = (date, cam)
    mtime = os.path.getmtime(imagepath)

    # the upload latency is recorded with respect to the time the image was
    # created, which is indicated by the image filename
    (yr, mo, day) = dir2date(createdatecam[0])
    (hr, minute, sec) = file2time(filename)
    fndt = datetime.datetime(yr, mo, day, hr, minute, sec)
    createminute = hr*60 + minute
    uplatdelta = datetime.datetime.fromtimestamp(mtime) - fndt
    uplat = uplatdelta.days*24*60 + float(uplatdelta.seconds)/60
    if uplat < 0:
        logging.warn("upload latency is negative: %s %s: %d minutes" % \
                         (createdatecam, filename, uplat))
        uplat = 0

    # datecam table for the image's creation date
    (lock, table) = lock_datecam(createdatecam)        
    row = table[createminute]
    if row[NCREATE] is None:
        row[NCREATE] = 0
    if row[AVGUPLAT] is None:
        row[AVGUPLAT] = 0.0
    row[AVGUPLAT] = (row[AVGUPLAT] * row[NCREATE] + uplat) / (row[NCREATE] + 1)
    row[NCREATE] += 1
    zeroback(table, createminute, NCREATE)
    lock.release()
    
    # per-server table for the image's creation date    datesrv = 
    (lock, table) = lock_datecam((createdatecam[0],""))
    row = table[createminute]
    if row[NCREATE] is None:
        row[NCREATE] = 0
    if row[AVGUPLAT] is None:
        row[AVGUPLAT] = 0.0
    row[AVGUPLAT] = (row[AVGUPLAT] * row[NCREATE] + uplat) / (row[NCREATE] + 1)
    row[NCREATE] += 1
    zeroback(table, createminute, NCREATE)
    lock.release()
    
    # the processing latency is recorded with respect the time the image was
    # uploaded to the server, which is indicated by the modification time of the
    # file
    now = time.time()
    proclat = (now - mtime)/60
    mtime_tm = time.localtime(mtime)
    uploaddatecam = (time.strftime("%Y-%m-%d", mtime_tm), createdatecam[1])
    uploadminute = mtime_tm.tm_hour*60 + mtime_tm.tm_min
    
    # datecam table for the image's upload date
    (lock, table) = lock_datecam(uploaddatecam)
    row = table[uploadminute]
    if row[NUPLOAD] is None:
        row[NUPLOAD] = 0
    if row[AVGPROCLAT] is None:
        row[AVGPROCLAT] = 0.0
    row[AVGPROCLAT] = (row[AVGPROCLAT] * row[NUPLOAD] + proclat) \
                        / (row[NUPLOAD] + 1)
    row[NUPLOAD] += 1
    zeroback(table, uploadminute, NUPLOAD)
    lock.release()
    
    # per-server table for the image's upload date
    (lock, table) = lock_datecam((uploaddatecam[0],""))
    row = table[uploadminute]
    if row[NUPLOAD] is None:
        row[NUPLOAD] = 0
    if row[AVGPROCLAT] is None:
        row[AVGPROCLAT] = 0.0
    row[AVGPROCLAT] = (row[AVGPROCLAT] * row[NUPLOAD] + proclat) \
                        / (row[NUPLOAD] + 1)
    row[NUPLOAD] += 1
    zeroback(table, uploadminute, NUPLOAD)
    lock.release()
    
    # the record the number of images processed this minute
    now_tm = time.localtime(now)
    nowdatecam = (time.strftime("%Y-%m-%d", now_tm), createdatecam[1])
    nowminute = now_tm.tm_hour*60 + now_tm.tm_min
    
    # datecam table for the image's processing date
    (lock, table) = lock_datecam(nowdatecam)
    if table[nowminute][NPROC] is None:
        table[nowminute][NPROC] = 0
    table[nowminute][NPROC] += 1
    zeroback(table, nowminute, NPROC)
    lock.release()

    # per-server table for the image's processing date
    (lock, table) = lock_datecam((nowdatecam[0],""))
    if table[nowminute][NPROC] is None:
        table[nowminute][NPROC] = 0
    table[nowminute][NPROC] += 1
    zeroback(table, nowminute, NPROC)
    lock.release()

def write_dctable(datecam):
    """Write the specified datecam or per-server
    stats table out to the filesystem."""
    is_ps = datecam[1]==""
    fp = os.path.join(statspath, datecam_to_fn(datecam)+".temp")
    statdict[datecam][LOCK].acquire()
    with open(fp, "wb") as csvfile:
        writer = csv.writer(csvfile, delimiter=',', quotechar='"')
        writer.writerow(PSCSVHEADERS if is_ps else DCCSVHEADERS)
        
        for m in range(MINPERDAY):
            trow = statdict[datecam][TABLE][m]
            csvrow = [None] * (LENPSROW if is_ps else LENDCROW + 1)
            csvrow[0] = datecam[0] + " %02d:%02d" % (m/60, m%60)
            csvrow[1:(LENPSROW if is_ps else LENDCROW)+1] = \
                [str(x) if x!=None else None for x in trow]
            writer.writerow(csvrow)
    dcfilepath = os.path.join(statspath, datecam_to_fn(datecam))
    if platform.system() == "Windows":    # :-P no atomic move & replace
        if os.path.isfile(dcfilepath):
            os.remove(dcfilepath)
    os.rename(fp, dcfilepath)
    statdict[datecam][CHANGED] = False
    statdict[datecam][LOCK].release()
    
def minute_stats(timestamp, cameras):
    global restarted
    # get count of unprocessed images for previous days and today.
    # Write each changed stats table out to the filesystem
    ts_tm = time.localtime(timestamp)
    today = time.strftime("%Y-%m-%d", ts_tm)
    minute = ts_tm.tm_hour*60 + ts_tm.tm_min
    datepaths = get_daydirs()
    unproctodayallcams = 0
    unprocprevdaysallcams = 0
    for cam in cameras:
        unproctoday = 0
        unprocprevdays = 0
        for dp in datepaths:
            dcp = os.path.join(dp, cam.shortname)
            if os.path.isdir(dcp):
                n = len(get_images_in_dir(dcp))
                (_, d) = os.path.split(dp)
                if d == today:
                    unproctoday += n
                else:
                    unprocprevdays += n
        unproctodayallcams += unproctoday
        unprocprevdaysallcams += unprocprevdays

        (lock, table) = lock_datecam((today, cam.shortname))
        table[minute][NUNPROC] = unproctoday
        table[minute][NUNPROCPREV] = unprocprevdays
        lock.release()

    # per-server table
    (lock, table) = lock_datecam((today, ""))
    table[minute][NUNPROC] = unproctodayallcams
    table[minute][NUNPROCPREV] = unprocprevdaysallcams
    if restarted:
        table[minute][RESTARTED] = 1
        restarted = False
    lock.release()

    for k in statdict.keys():
        if statdict[k][CHANGED]:
            write_dctable(k)

def restart_stats():
    global restarted
    if not os.path.isdir(statspath):
        os.mkdir(statspath)
    restarted = True

def expire_stats(retain_days):
    """Retain the number of days of stats files specified by retain_days,
    if extant, and remove any stats files from earlier days."""
    files = os.listdir(statspath)
    # find stats files; the .* at the end of the re picks up stats .temp files
    mobjs = [re.search(r"^(\d\d\d\d-\d\d-\d\d)_.*\.csv.*",f) for f in files]
    statsdates = sorted(list(set([m.group(1) for m in mobjs if m])))
    # if there are more stats file dates than the number we're supposed to
    # retain, delete the out-of-date files
    if len(statsdates) > retain_days:
        # find the earliest date for which stats should be retained
        retain_date = statsdates[-retain_days]
        # delete the stats files earlier than the retain date
        for f in sorted([m.group(0) for m in mobjs if m]):
            if re.search("^"+retain_date, f):
                break
            os.remove(os.path.join(statspath, f))
    
# Flag to stop the stats loop for test purposes.
# Only for manipulation by testing code; always set to False in this file
#
terminate_stats_loop = False 
    
def stats_loop(cameras):
    """Called by stats thread to run the per-minute stats processing.  cameras
    is the list of camera objects."""
    set_thread_prefix(threading.current_thread(), "Stats")
    logging.info("Starting stats_loop()")
    while True:
        ts = time.time()
        time.sleep(60 - ts%60)
        minute_stats(time.time(), cameras)
        if terminate_stats_loop:
            return

