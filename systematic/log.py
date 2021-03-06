# coding=utf-8
"""
Logging from scripts and parser for syslog files
"""

import os
import sys
import fnmatch
import re
import urllib
import bz2
import gzip
import threading
import logging
import logging.handlers

from datetime import datetime, timedelta

from systematic.tail import TailReader, TailReaderError

DEFAULT_LOGFORMAT = '%(module)s %(levelname)s %(message)s'
DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOGFILEFORMAT = '%(asctime)s %(module)s.%(funcName)s %(message)s'
DEFAULT_LOGSIZE_LIMIT = 2**20
DEFAULT_LOG_BACKUPS = 10

DEFAULT_SYSLOG_FORMAT = '%(message)s'
DEFAULT_SYSLOG_LEVEL =  logging.handlers.SysLogHandler.LOG_WARNING
DEFAULT_SYSLOG_FACILITY = logging.handlers.SysLogHandler.LOG_USER

# Mapping to set syslog handler levels via same classes as normal handlers
LOGGING_LEVEL_NAMES = ( 'DEBUG', 'INFO', 'WARN', 'ERROR', 'CRITICAL')
SYSLOG_LEVEL_MAP = {
    logging.handlers.SysLogHandler.LOG_DEBUG:   logging.DEBUG,
    logging.handlers.SysLogHandler.LOG_NOTICE:  logging.INFO,
    logging.handlers.SysLogHandler.LOG_INFO:    logging.INFO,
    logging.handlers.SysLogHandler.LOG_WARNING: logging.WARN,
    logging.handlers.SysLogHandler.LOG_ERR:     logging.ERROR,
    logging.handlers.SysLogHandler.LOG_CRIT:    logging.CRITICAL,
}

# Local syslog device varies by platform
if sys.platform == 'linux2' or fnmatch.fnmatch(sys.platform, '*bsd*'):
    DEFAULT_SYSLOG_ADDRESS = '/dev/log'
elif sys.platform == 'darwin':
    DEFAULT_SYSLOG_ADDRESS = '/var/run/syslog'
else:
    DEFAULT_SYSLOG_ADDRESS = ( 'localhost', 514 )


# Default matchers for syslog entry 'host, program, pid' parts
SOURCE_FORMATS = [
    re.compile('^<(?P<version>[^>]+)>\s+(?P<host>[^\s]+)\s+(?P<program>[^\[]+)\[(?P<pid>\d+)\]$'),
    re.compile('^<(?P<version>[^>]+)>\s+(?P<host>[^\s]+)\s+(?P<program>[^\[]+)$'),
    re.compile('^<(?P<facility>\d+)\.(?P<level>\d+)>\s+(?P<host>[^\s]+)\s+(?P<program>[^\[]+)\[(?P<pid>\d+)\]$'),
    re.compile('^<(?P<facility>\d+)\.(?P<level>\d+)>\s+(?P<host>[^\s]+)\s+(?P<program>[^\[]+)$'),
    re.compile('^(?P<host>[^\s]+)\s+(?P<program>[^\[]+)\[(?P<pid>\d+)\]$'),
    re.compile('^(?P<host>[^\s]+)\s+(?P<program>[^\[]+)$'),
]


class LoggerError(Exception):
    """
    Exceptions raised by logging configuration
    """
    pass


class Logger(object):
    """
    Singleton class for common logging tasks.
    """
    __instances = {}
    def __init__(self, name=None, logformat=DEFAULT_LOGFORMAT, timeformat=DEFAULT_TIME_FORMAT):
        name = name is not None and name or self.__class__.__name__
        thread_id = threading.current_thread().ident
        if thread_id is not None:
            name = '{0:d}-{1}'.format(thread_id, name)

        if not Logger.__instances.has_key(name):
            Logger.__instances[name] = Logger.LoggerInstance(name, logformat, timeformat)

        self.__dict__['_Logger__instances'] = Logger.__instances
        self.__dict__['name'] = name

    class LoggerInstance(dict):
        """
        Singleton implementation of logging configuration for one program
        """
        def __init__(self, name, logformat, timeformat):
            self.name = name
            self.level = logging.Logger.root.level
            self.register_stream_handler('default_stream', logformat, timeformat)

        def __getattr__(self, attr):
            if attr in self.keys():
                return self[attr]
            raise AttributeError('No such LoggerInstance log handler: {0}'.format(attr))

        def __get_or_create_logger__(self, name):
            if name not in self.keys():
                for logging_manager in logging.Logger.manager.loggerDict.values():
                    if hasattr(logging_manager, 'name') and logging_manager.name==name:
                        self[name] = logging.getLogger(name)
                        break

            if name not in self.keys():
                self[name] = logging.getLogger(name)

            return self[name]

        def __match_handlers__(self, handler_list, handler):
            def match_handler(a, b):
                if type(a) != type(b):
                    return False

                if isinstance(a, logging.StreamHandler):
                    for k in ('stream', 'name',):
                        if getattr(a, k) != getattr(b, k):
                            return False
                    return True

                if isinstance(a, logging.handlers.SysLogHandler):
                    for k in ( 'address', 'facility', ):
                        if getattr(a, k) != getattr(b, k):
                            return False
                    return True

                if isinstance(a, logging.handlers.HTTPHandler):
                    for k in ( 'host', 'url', 'method', ):
                        if getattr(a, k) != getattr(b, k):
                            return False
                    return True

                return True

            if not isinstance(handler, logging.Handler):
                raise LoggerError('Not an instance of logging.Handler: {0}'.format(handler))

            if not isinstance(handler_list, list):
                raise LoggerError('BUG handler_list must be a list instance')

            for match in handler_list:
                if match_handler(match, handler):
                    return True

            return False

        def register_stream_handler(self, name, logformat=None, timeformat=None):
            if logformat is None:
                logformat = DEFAULT_LOGFORMAT
            if timeformat is None:
                timeformat = DEFAULT_TIME_FORMAT

            logger = self.__get_or_create_logger__(name)
            handler = logging.StreamHandler()
            if self.__match_handlers__(self.default_stream.handlers, handler):
                return

            if not self.__match_handlers__(logger.handlers, handler):
                handler.setFormatter(logging.Formatter(logformat, timeformat))
                logger.addHandler(handler)

            return logger

        def register_syslog_handler(self, name,
                address=DEFAULT_SYSLOG_ADDRESS,
                facility=DEFAULT_SYSLOG_FACILITY,
                default_level=DEFAULT_SYSLOG_LEVEL,
                socktype=None,
                logformat=None,
            ):

            if logformat is None:
                logformat = DEFAULT_SYSLOG_FORMAT

            if default_level not in SYSLOG_LEVEL_MAP.keys():
                raise LoggerError('Unsupported syslog level value')

            logger = self.__get_or_create_logger__(name)
            handler = logging.handlers.SysLogHandler(address, facility, socktype)
            handler.level = default_level
            if not self.__match_handlers__(logger.handlers, handler):
                handler.setFormatter(logging.Formatter(logformat))
                logger.addHandler(handler)
                logger.setLevel(self.loglevel)

            return logger

        def register_http_handler(self, name, url, method='POST'):
            logger = self.__get_or_create_logger__(name)
            try:
                host, path = urllib.splithost(url[url.index(':')+1:])
            except IndexError, emsg:
                raise LoggerError('Error parsing URL {0}: {1}'.format(url, emsg))

            handler = logging.handlers.HTTPHandler(host, url, method)
            if not self.__match_handlers__(logger.handlers, handler):
                logger.addHandler(handler)
                logger.setLevel(self.loglevel)

            return logger

        def register_file_handler(self, name, directory,
                         filename=None,
                         logformat=None,
                         timeformat=None,
                         maxBytes=DEFAULT_LOGSIZE_LIMIT,
                         backupCount=DEFAULT_LOG_BACKUPS):

            if filename is None:
                filename = '{0}.log'.format(name)
            if logformat is None:
                logformat = DEFAULT_LOGFILEFORMAT
            if timeformat is None:
                timeformat = DEFAULT_TIME_FORMAT

            if not os.path.isdir(directory):
                try:
                    os.makedirs(directory)
                except OSError:
                    raise LoggerError('Error creating directory: {0}'.format(directory))
            logfile = os.path.join(directory, filename)

            logger = self.__get_or_create_logger__(name)
            handler = logging.handlers.RotatingFileHandler(
                filename=logfile,
                mode='a+',
                maxBytes=maxBytes,
                backupCount=backupCount
            )
            if not self.__match_handlers__(logger.handlers, handler):
                handler.setFormatter(logging.Formatter(logformat, timeformat))
                logger.addHandler(handler)
                logger.setLevel(self.loglevel)

            return logger

        @property
        def level(self):
            return self._level
        @level.setter
        def level(self, value):
            if not isinstance(value, int):
                if value in LOGGING_LEVEL_NAMES:
                    value = getattr(logging, value)
                try:
                    value = int(value)
                    if value not in SYSLOG_LEVEL_MAP.values():
                        raise ValueError
                except ValueError:
                    raise ValueError('Invalid logging level value: {0}'.format(value))

            for logger in self.values():
                if hasattr(logger, 'setLevel'):
                    logger.setLevel(value)
            self._level = value

        # Compatibility for old API
        @property
        def loglevel(self):
            return self.level
        @loglevel.setter
        def loglevel(self, value):
            self.level = value

        # compatibility for old API
        def set_level(self, value):
            self.level = value

    def __getattr__(self, attr):
        return getattr(self.__instances[self.name], attr)

    def __setattr__(self, attr, value):
        setattr(self.__instances[self.name], attr, value)

    def __getitem__(self, item):
        return self.__instances[self.name][item]

    def __setitem__(self, item, value):
        self.__instances[self.name][item] = value

    def register_stream_handler(self, name, logformat=None, timeformat=None):
        """
        Register a common log stream handler
        """
        return self.__instances[self.name].register_stream_handler(
            name, logformat, timeformat
        )

    def register_syslog_handler(self, name,
            address=DEFAULT_SYSLOG_ADDRESS,
            facility=DEFAULT_SYSLOG_FACILITY,
            default_level=DEFAULT_SYSLOG_LEVEL,
            socktype=None,
            logformat=None,
        ):
        """Register syslog handler

        Register handler for syslog messages

        """
        return self.__instances[self.name].register_syslog_handler(
            name, address, facility, default_level, socktype, logformat
        )

    def register_http_handler(self, name, url, method='POST'):
        """Register HTTP handler

        Register a HTTP POST logging handler

        """
        return self.__instances[self.name].register_http_handler(
            name, url, method
        )

    def register_file_handler(self, name, directory,
                     filename=None,
                     logformat=None,
                     timeformat=None,
                     maxBytes=DEFAULT_LOGSIZE_LIMIT,
                     backupCount=DEFAULT_LOG_BACKUPS):
        """Register log file handler

        Register a common log file handler for rotating file based logs

        """
        return self.__instances[self.name].register_file_handler(
            name, directory, filename, logformat, timeformat, maxBytes, backupCount
        )


class LogFileError(Exception):
    """
    Exceptions from logfile parsers
    """
    pass


class LogEntry(object):
    """
    Generic syslog logfile entry
    """
    def __init__(self, logfile, line, year, source_formats):
        line = line.rstrip()
        self.logfile = logfile
        self.line = line
        self.message_fields = {}

        self.version = None
        self.host = None
        self.program = None
        self.pid = None

        try:
            mon, day, time, line = line.split(None, 3)
        except ValueError:
            raise LogFileError('Error splitting log line: {0}'.format(self.line))

        try:
            self.time = datetime.strptime('{0} {1} {2} {3}'.format(year, mon, day, time), '%Y %b %d %H:%M:%S')
        except ValueError:
            raise LogFileError('Error parsing entry time from line: {0}'.format(self.line))

        try:
            self.source, self.message = [x.strip() for x in line.split(':', 1)]
            for fmt in source_formats:
                m = fmt.match(self.source)
                if m:
                    for k, v in m.groupdict().items():
                        setattr(self, k, v)
                    break

        except ValueError:
            # Lines like '--- last message repeated 2 times ---'
            self.source = None
            self.message = line

    def __repr__(self):
        return '{0} {1}{2}{3}'.format(
            self.time.strftime('%Y-%m-%d %H:%M:%S'),
            self.program is not None and '{0} '.format(self.program) or '',
            self.pid is not None and '({0}) '.format(self.pid) or '',
            self.message
        )

    def append(self, message):
        self.message = '{0}\n{1}'.format(self.message, message.rstrip())

    def update_message_fields(self, data):
        self.message_fields.update(data)


class LogFile(list):
    """
    Generic syslog file parser
    """
    lineloader = LogEntry
    def __init__(self, path, source_formats=SOURCE_FORMATS):
        if isinstance(path, basestring):
            self.path = os.path.expanduser(os.path.expandvars(path))
        else:
            self.path = path

        self.source_formats = source_formats
        self.mtime = None

        self.iterators = {}
        self.register_iterator('default')

        self.__loaded = False
        self.fd = None

    def __repr__(self):
        return '{0} {1} entries'.format(self.path, len(self))

    def __iter__(self):
        return self

    def register_iterator(self, name):
        if name in self.iterators:
            raise LogFileError('Iterator name already registered: {0}'.format(name))
        self.iterators[name] = None

    def get_iterator(self, name):
        if name not in self.iterators:
            raise LogFileError('Iterator name not registered: {0}'.format(name))

        if self.iterators[name] is None:
            self.iterators[name] = 0

        return self.iterators[name]

    def reset_iterator(self, name):
        if name not in self.iterators:
            raise LogFileError('Iterator name not registered: {0}'.format(name))

        self.iterators[name] = None

    def update_iterator(self, name, value=None):
        if name not in self.iterators:
            raise LogFileError('Iterator name not registered: {0}'.format(name))

        if value is not None:
            self.iterators[name] = value

        else:
            if self.iterators[name] is not None:
                self.iterators[name] += 1
            else:
                self.iterators[name] = 0

    def __open_logfile__(self, path):
        """Try opening log file

        Try opening logfile in gz, bz2 and raw text formats

        """
        if not os.path.isfile(path):
            raise LogFileError('No such file: {0}'.format(path))

        try:
            fd = gzip.GzipFile(path)
            fd.readline()
            fd.seek(0)
            return fd
        except IOError, emsg:
            pass

        try:
            fd = bz2.BZ2File(path)
            fd.readline()
            fd.seek(0)
            return fd
        except IOError:
            pass

        try:
            fd = open(path, 'r')
            fd.readline()
            fd.seek(0)
            return fd
        except IOError:
            pass

        raise LogFileError('Error opening logfile {0}'.format(path))

    def next_iterator_match(self, iterator, callback=None):
        if iterator not in self.iterators:
            raise LogFileError('Unknown iterator: {0}'.format(iterator))

        if not self.__loaded:
            if self.fd == None:
                if isinstance(self.path, file):
                    self.fd = self.path
                    self.mtime = datetime.now()
                else:
                    try:
                        self.fd = self.__open_logfile__(self.path)
                        self.mtime = datetime.fromtimestamp(os.stat(self.path).st_mtime)
                    except OSError, (ecode, emsg):
                        raise LogFileError('Error opening {0}: {1}'.format(self.path, emsg))

            while True:
                if self.get_iterator(iterator) < len(self)-1:
                    try:
                        entry = self[self.get_iterator(iterator)]
                    except IndexError:
                        self.reset_iterator(iterator)
                        raise StopIteration

                else:
                    entry = self.readline()

                if entry is None:
                    self.reset_iterator(iterator)
                    raise StopIteration

                self.update_iterator(iterator, len(self)-1)
                if callback is not None:
                    if callback(entry):
                        return entry
                else:
                    return entry

        else:
            # Iterate cached entries
            if self.iterators[iterator] is None:
                self.update_iterator(iterator)

            while True:
                try:
                    entry = self[self.get_iterator(iterator)]
                    self.update_iterator(iterator)

                    if callback is not None:
                        if callback(entry):
                            return entry
                    else:
                        return entry

                except IndexError:
                    self.reset_iterator(iterator)
                    raise StopIteration

    def next(self):
        """Next iterator

        Standard iterator next() call

        """
        return self.next_iterator_match(iterator='default')

    def readline(self):
        """
        Parse entry from logfile
        """
        if self.fd is None:
            raise LogFileError('File was not loaded')
        try:
            l = self.fd.readline()
            if l == '':
                self.__loaded = True
                return None

            # Multiline log entry
            if l[:1] in [' ', '\t'] and len(self):
                self[-1].append(l)
                return self.readline()

            else:
                entry = self.lineloader(self, l,
                    year=self.mtime.year,
                    source_formats=self.source_formats
                )
                self.append(entry)
                return entry

        except OSError, (ecode, emsg):
            raise LogFileError('Error reading file {0}: {1}'.format(self.path, emsg))

    def reload(self):
        """Reload file

        Reload file, clearing existing entries

        """
        self.__delslice__(0, len(self))
        self.__loaded = False
        while True:
            try:
                entry = self.next()
            except StopIteration:
                break

    def filter_host(self, host):
        """Filter by host name

        Return log entries matching given host name

        """
        if len(self) == 0:
            self.reload()
        return [x for x in self if x.host == host]

    def filter_program(self, program):
        """Filter by program name

        Return log entries matching given program name

        """
        if len(self) == 0:
            self.reload()
        return [x for x in self if x.program == program]

    def filter_message(self, message_regexp):
        """Filter by message regexp

        Filter log entries matching given regexp in message field

        """
        if len(self) == 0:
            self.reload()

        if isinstance(message_regexp, basestring):
            message_regexp = re.compile(message_regexp)

        return [x for x in self if message_regexp.match(x.message)]

    def match_message(self, message_regexp):
        """

        Return dictionary of matching regexp keys for lines matching given regexp
         in message field

        """
        if len(self) == 0:
            self.reload()

        if isinstance(message_regexp, basestring):
            message_regexp = re.compile(message_regexp)

        matches = []
        for x in self:
            m = message_regexp.match(x.message)
            if not m:
                continue
            matches.append(m.groupdict())
        return matches


class LogFileCollection(object):
    """Process multiple logfiles

    Load, iterate and process multiple log files. Typical usage would be like:

        lc = LogFileCollection(glob.glob('/var/log/auth.log*'))

    Files are sorted by modification timestamp and name.

    """
    loader = LogFile
    def __init__(self, logfiles, source_formats=SOURCE_FORMATS):
        self.source_formats = source_formats
        self.logfiles = []
        self.__iter_index = None
        self.__iter_entry = None

        stats = {}
        for path in logfiles:
            try:
                st = os.stat(path)
                ts = long(st.st_mtime)

                if ts not in stats.keys():
                    stats[ts] = []
                stats[ts].append(path)

            except OSError, (ecode, emsg):
                raise LogFileError('Error running stat on {0}: {1}'.format(path, emsg))
            except IOError, (ecode, emsg):
                raise LogFileError('Error running stat on {0}: {1}'.format(path, emsg))

        for ts in stats.keys():
            stats[ts].sort()

        for ts in sorted(stats.keys()):
            self.logfiles.extend(
                self.loader(path, source_formats=self.source_formats) for path in stats[ts]
            )

    def __repr__(self):
        return 'collection of {0:d} logfiles'.format(len(self.logfiles))

    def __iter__(self):
        return self

    def next(self):
        if not self.logfiles:
            raise StopIteration

        if self.__iter_index is None:
            self.__iter_index = 0
            self.__iter_entry = self.logfiles[0]

        try:
            logentry = self.__iter_entry.next()

        except StopIteration, emsg:
            if self.__iter_index < len(self.logfiles) - 1:
                self.__iter_index += 1
                self.__iter_entry = self.logfiles[self.__iter_index]

                try:
                    logentry = self.__iter_entry.next()
                except StopIteration:
                    self.__iter_index = None
                    self.__iter_entry = None
                    raise StopIteration

            else:
                self.__iter_index = None
                self.__iter_entry = None
                raise StopIteration

        return logentry

    def filter_host(self, host):
        """Filter by host

        Filter all loaded logfiles by matching host with LogFile.filter_host

        """
        matches = []
        for parser in self.logfiles:
            matches.extend(parser.filter_host(host))
        return matches

    def filter_program(self, program):
        """Filter by program

        Filter all loaded logfiles by matching program with LogFile.filter_program

        """
        matches = []
        for parser in self.logfiles:
            matches.extend(parser.filter_program(program))
        return matches

    def filter_message(self, message_regexp):
        """Filter messages by regexp

        Match all loaded logfiles by matching program with LogFile.filter_message

        """
        if isinstance(message_regexp, basestring):
            message_regexp = re.compile(message_regexp)

        matches = []
        for parser in self.logfiles:
            matches.extend(parser.filter_message(message_regexp))
        return matches

    def match_message(self, message_regexp):
        """Match messages by regexp

        Match all loaded logfiles by matching program with LogFile.match_message

        """
        if isinstance(message_regexp, basestring):
            message_regexp = re.compile(message_regexp)

        matches = []
        for parser in self.logfiles:
            matches.extend(parser.match_message(message_regexp))
        return matches


class LogfileTailReader(TailReader):
    """Logfile tail reader

    Tail reader returning LogFile entries

    """
    def __init__(self, path=None, fd=None, source_formats=SOURCE_FORMATS, lineparser=LogEntry):
        super(LogfileTailReader, self).__init__(wpath, fd)
        self.source_formats = source_formats
        self.lineparser = lineparser

    def __format_line__(self, line):
         return self.lineparser(line[:-1], self.year, source_formats=self.source_formats)

