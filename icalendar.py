"""Definitions and behavior for iCalendar, also known as vCalendar 2.0"""

import string
import behavior
import dateutil.rrule
import dateutil.tz
import StringIO
import datetime
import socket, random #for generating a UID
import itertools

from vobject import VObjectError, NativeError, ValidateError, ParseError, \
                    VBase, Component, ContentLine, logger, defaultSerialize, \
                    registerBehavior, backslashEscape

#------------------------------- Constants -------------------------------------
DATENAMES = ("rdate", "exdate")
RULENAMES = ("exrule", "rrule")
DATESANDRULES = ("exrule", "rrule", "rdate", "exdate")
PRODID = u"-//PYVOBJECT//NONSGML Version 1//EN"

WEEKDAYS = "MO", "TU", "WE", "TH", "FR", "SA", "SU"
FREQUENCIES = ('YEARLY', 'MONTHLY', 'WEEKLY', 'DAILY', 'HOURLY', 'MINUTELY',
               'SECONDLY')

#---------------------------- TZID registry ------------------------------------
__tzidMap={}

def registerTzid(tzid, tzinfo):
    """Register a tzid -> tzinfo mapping."""
    __tzidMap[tzid]=tzinfo

def getTzid(tzid):
    """Return the tzid if it exists, or None."""
    return __tzidMap.get(tzid, None)

utc = dateutil.tz.tzutc()
registerTzid("UTC", utc)

#-------------------- Helper subclasses ----------------------------------------

class TimezoneComponent(Component):
    """A VTIMEZONE object.
    
    VTIMEZONEs are parsed by dateutil.tz.tzical, the resulting datetime.tzinfo
    subclass is stored in self.tzinfo, self.tzid stores the TZID associated
    with this timezone.
    
    @ivar name:
        The uppercased name of the object, in this case always 'VTIMEZONE'.
    @ivar tzinfo:
        A datetime.tzinfo subclass representing this timezone.
    @ivar tzid:
        The string used to refer to this timezone.
    
    """

    def __init__(self, tzinfo=None, *args, **kwds):
        """Accept an existing Component or a tzinfo class."""
        super(TimezoneComponent, self).__init__(*args, **kwds)
        self.isNative=True
        # hack to make sure a behavior is assigned
        if self.behavior is None:
            self.behavior = VTimezone
        if tzinfo is not None:
            self.tzinfo = tzinfo
        if not hasattr(self, 'name') or self.name == '':
            self.name = 'VTIMEZONE'
            self.useBegin = True

    def __setattr__(self, name, value):
        if name == 'tzinfo':
            self.settzinfo(value)
        else:
            super(TimezoneComponent, self).__setattr__(name, value)

    @classmethod
    def registerTzinfo(obj, tzinfo):
        tzid = obj.pickTzid(tzinfo)
        if tzid and not getTzid(tzid):
            registerTzid(tzid, tzinfo)

    def gettzinfo(self):
        # use defaultSerialize rather than self.serialize to avoid infinite
        # loop of TransformFromNative -> transformToNative -> gettzinfo
        buffer = StringIO.StringIO(str(defaultSerialize(self, None, 75)))
        return dateutil.tz.tzical(buffer).get()
        
    tzinfo = property(gettzinfo)
    
    def settzinfo(self, tzinfo, start=2000, end=2030):
        """Create appropriate objects in self to represent tzinfo.
        
        Assumptions:
        - DST <-> Standard transitions occur on the hour
        - never within a month of one another
        - twice or fewer times a year
        - never in the month of December
        - DST always moves offset exactly one hour later
        - tzinfo classes dst method always treats times that could be in either
          offset to be in the later regime
        
        """
        # tests of whether the given key is in effect
        zeroDelta = datetime.timedelta(0)
        tests = {'daylight' : lambda dt: tzinfo.dst(dt) != zeroDelta,
                 'standard' : lambda dt: tzinfo.dst(dt) == zeroDelta}
                    
        def firstTransition(iterDates, test):
            """
            Return the last date not matching test, or None if all tests matched.
            """
            success = None
            for dt in iterDates:
                if not test(dt):
                    success = dt
                else:
                    if success is not None:
                        return success
            return success # may be None
    
        def generateDates(year, month=None, day=None):
            """Iterate over possible dates with unspecified values."""
            months = range(1, 13)
            days   = range(1, 32)
            hours  = range(0, 24)
            if month is None:
                for month in months:
                    yield datetime.datetime(year, month, 1)
            elif day is None:
                for day in days:
                    try:
                        yield datetime.datetime(year, month, day)
                    except ValueError:
                        pass
            else:
                for hour in hours:
                    yield datetime.datetime(year, month, day, hour)
            
        def fromLastWeek(dt):
            """How many weeks from the end of the month dt is, starting from 1."""
            weekDelta = datetime.timedelta(weeks=1)
            n = 1
            current = dt + weekDelta
            while current.month == dt.month:
                n += 1
                current += weekDelta
            return n
        
        def getTransitionOccurrence(year, month, dayofweek, n, hour):
            weekday = dateutil.rrule.weekday(dayofweek, n)
            if hour is None:
                # all year offset, with no rule
                return datetime.datetime(year, 1, 1)
            rule = dateutil.rrule.rrule(dateutil.rrule.YEARLY,
                                        bymonth = month,
                                        byweekday = weekday,
                                        dtstart = datetime.datetime(year, 1, 1, hour))
            return rule[0]
        
        # lists of dictionaries defining rules which are no longer in effect
        completed = {'daylight' : [], 'standard' : []}
    
        # dictionaries defining rules which are currently in effect
        working   = {'daylight' : None, 'standard' : None}
            
        # rule may be based on the nth day of the month or the nth from the last
        for year in xrange(start, end + 1):
            newyear = datetime.datetime(year, 1, 1)
            for transitionTo in 'daylight', 'standard':
                oldrule = working[transitionTo]
    
                test = tests[transitionTo]
                monthDt = firstTransition(generateDates(year), test)
                if monthDt is None:
                    # transitionTo is in effect for the whole year
                    yearStart = datetime.datetime(year, 1, 1)
                    rule = {'end'        : None,
                            'start'      : yearStart,
                            'month'      : 1,
                            'weekday'    : None,
                            'hour'       : None,
                            'plus'       : None,
                            'minus'      : None,
                            'name'       : tzinfo.tzname(yearStart),
                            'offset'     : tzinfo.utcoffset(yearStart),
                            'offsetfrom' : tzinfo.utcoffset(yearStart)}
                    if oldrule is None:
                        # transitionTo was not yet in effect
                        working[transitionTo] = rule
                    else:
                        # transitionTo was already in effect
                        if (oldrule['offset'] != 
                            tzinfo.utcoffset(yearStart)):
                            # old rule was different, it shouldn't continue
                            oldrule['end'] = year - 1
                            completed[transitionTo].append(oldrule)
                            working[transitionTo] = rule
                    continue
    
                elif monthDt.month == 12:
                    # transitionTo is not in effect
                    if oldrule is not None:
                        # transitionTo used to be in effect
                        oldrule['end'] = year - 1
                        completed[transitionTo].append(oldrule)
                        working[transitionTo] = None
                    continue
                else:
                    # an offset transition was found
                    month = monthDt.month
                
                # there was a good transition somewhere in a non-December month
                day         = firstTransition(generateDates(year, month), test).day
                uncorrected = firstTransition(generateDates(year, month, day), test)
                
                if transitionTo == 'standard':
                    # assuming tzinfo.dst returns a new offset for the first
                    # possible hour, we need to add one hour for the offset change
                    # and another hour because firstTransition returns the hour
                    # before the transition
                    corrected = uncorrected + datetime.timedelta(hours=2)
                else:
                    corrected = uncorrected + datetime.timedelta(hours=1)

                rule = {'end'     : None, # None, or an integer year
                        'start'   : corrected, # the datetime of transition
                        'month'   : corrected.month,
                        'weekday' : corrected.weekday(),
                        'hour'    : corrected.hour,
                        'name'    : tzinfo.tzname(corrected),
                        'plus'    : (corrected.day - 1)/ 7 + 1,#nth week of the month
                        'minus'   : fromLastWeek(corrected), #nth from last week
                        'offset'  : tzinfo.utcoffset(corrected), 
                        'offsetfrom' : tzinfo.utcoffset(uncorrected)}
    
                if oldrule is None: 
                    working[transitionTo] = rule
                else:
                    plusMatch  = rule['plus']  == oldrule['plus'] 
                    minusMatch = rule['minus'] == oldrule['minus'] 
                    truth = plusMatch or minusMatch
                    for key in 'month', 'weekday', 'hour', 'offset':
                        truth = truth and rule[key] == oldrule[key]
                    if truth:
                        # the old rule is still true, limit to plus or minus
                        if not plusMatch:
                            oldrule['plus'] = None
                        if not minusMatch:
                            oldrule['minus'] = None
                    else:
                        # the new rule did not match the old
                        oldrule['end'] = year - 1
                        completed[transitionTo].append(oldrule)
                        working[transitionTo] = rule
    
        for transitionTo in 'daylight', 'standard':
            if working[transitionTo] is not None:
                completed[transitionTo].append(working[transitionTo])
    
        self.tzid = []
        self.daylight = []
        self.standard = []
        
        self.add('tzid').value = self.pickTzid(tzinfo)
        
        old = None
        for transitionTo in 'daylight', 'standard':
            for rule in completed[transitionTo]:
                comp = self.add(transitionTo)
                dtstart = comp.add('dtstart')
                dtstart.value = rule['start']
                comp.add('tzname').value  = rule['name']
                line = comp.add('tzoffsetto')
                line.value = deltaToOffset(rule['offset'])
                line = comp.add('tzoffsetfrom')
                line.value = deltaToOffset(rule['offsetfrom'])
    
                if rule['plus'] is not None:
                    num = rule['plus']
                elif rule['minus'] is not None:
                    num = -1 * rule['minus']
                else:
                    num = None
                if num is not None:
                    dayString = ";BYDAY=" + str(num) + WEEKDAYS[rule['weekday']]
                else:
                    dayString = ""
                if rule['end'] is not None:
                    endDate = getTransitionOccurrence(rule['end'],
                                                      rule['month'],
                                                      rule['weekday'],
                                                      num,
                                                      rule['hour'])
                    endDate = endDate.replace(tzinfo = utc) - rule['offsetfrom']
                    endString = ";UNTIL="+ dateTimeToString(endDate)
                else:
                    endString = ''
                rulestring = "FREQ=YEARLY%s;BYMONTH=%s%s" % \
                              (dayString, str(rule['month']), endString)
                
                comp.add('rrule').value = rulestring                

    
    @staticmethod
    def pickTzid(tzinfo):
        """
        Given a tzinfo class, use known APIs to determine TZID, or use tzname.
        """
        if tzinfo is None or tzinfo == utc:
            #If tzinfo is UTC, we don't need a TZID
            return None
        # try PyICU's tzid key
        if hasattr(tzinfo, 'tzid'):
            return tzinfo.tzid

        # try tzical's tzid key
        elif hasattr(tzinfo, '_tzid'):
            return tzinfo._tzid
        else:
            # return tzname for standard (non-DST) time
            notDST = datetime.timedelta(0)
            for month in xrange(1,13):
                dt = datetime.datetime(2000, month, 1)
                if tzinfo.dst(dt) == notDST:
                    return tzinfo.tzname(dt)
        # there was no standard time in 2000!
        raise VObjectError("Unable to guess TZID for tzinfo %s" % str(tzinfo))

    def __str__(self):
        return "<VTIMEZONE | " + str(getattr(self, 'tzid', ['No TZID'])[0]) +">"
    
    def __repr__(self):
        return self.__str__()
    
    def prettyPrint(self, level, tabwidth):
        pre = ' ' * level * tabwidth
        print pre, self.name
        print pre, "TZID:", self.tzid[0]
        print

class RecurringComponent(Component):
    """A vCalendar component like VEVENT or VTODO which may recur.
        
    Any recurring component can have one or multiple RRULE, RDATE,
    EXRULE, or EXDATE lines, and one or zero DTSTART lines.  It can also have a
    variety of children that don't have any recurrence information.  
    
    In the example below, note that dtstart is included in the rruleset.
    This is not the default behavior for dateutil's rrule implementation unless
    dtstart would already have been a member of the recurrence rule, and as a
    result, COUNT is wrong. This can be worked around when getting rruleset by
    adjusting count down by one if an rrule has a count and dtstart isn't in its
    result set, but by default, the rruleset property doesn't do this work
    around, to access it getrruleset must be called with addRDate set True.
    
    When creating rrule's programmatically it should be kept in
    mind that count doesn't necessarily mean what rfc2445 says.
    
    >>> import dateutil.rrule, datetime
    >>> vevent = RecurringComponent(name='VEVENT')
    >>> vevent.add('rrule').value =u"FREQ=WEEKLY;COUNT=2;INTERVAL=2;BYDAY=TU,TH"
    >>> vevent.add('dtstart').value = datetime.datetime(2005, 1, 19, 9)
    >>> list(vevent.rruleset)
    [datetime.datetime(2005, 1, 20, 9, 0), datetime.datetime(2005, 2, 1, 9, 0)]
    >>> list(vevent.getrruleset(True))
    [datetime.datetime(2005, 1, 19, 9, 0), datetime.datetime(2005, 1, 20, 9, 0)]
    
    @ivar rruleset:
        A U{rruleset<https://moin.conectiva.com.br/DateUtil>}.
    """
    def __init__(self, *args, **kwds):
        super(RecurringComponent, self).__init__(*args, **kwds)
        self.isNative=True
        #self.clobberedRDates=[]


    def getrruleset(self, addRDate = False):
        """Get an rruleset created from self.
        
        If addRDate is True, add an RDATE for dtstart if it's not included in
        an RRULE, and count is decremented if it exists.
        
        Note that for rules which don't match DTSTART, DTSTART may not appear
        in list(rruleset), although it should.  By default, an RDATE is not
        created in these cases, and count isn't updated, so dateutil may list
        a spurious occurrence.
        
        """
        rruleset = None
        for name in DATESANDRULES:
            addfunc = None
            for line in self.contents.get(name, ()):
                # don't bother creating a rruleset unless there's a rule
                if rruleset is None:
                    rruleset = dateutil.rrule.rruleset()
                if addfunc is None:
                    addfunc=getattr(rruleset, name)
                
                if name in DATENAMES:
                    if type(line.value[0]) == datetime.datetime:
                        map(addfunc, line.value)
                    elif type(line.value) == datetime.date:
                        for dt in line.value:
                            addfunc(datetime.datetime(dt.year, dt.month, dt.day))
                    else:
                        # ignore RDATEs with PERIOD values for now
                        pass
                elif name in RULENAMES:
                    try:
                        dtstart = self.dtstart[0].value
                    except AttributeError, KeyError:
                        # if there's no dtstart, just return None
                        return None
                    # rrulestr complains about unicode, so cast to str
                    addfunc(dateutil.rrule.rrulestr(str(line.value),
                                                    dtstart=dtstart))
                    if name == 'rrule' and addRDate:
                        try:
                            if rruleset._rrule[-1][0] != dtstart:
                                rruleset.rdate(dtstart)
                                added = True
                        except IndexError:
                            # it's conceivable that an rrule might have 0 datetimes
                            added = False
                        if added and rruleset._rrule[-1]._count != None:
                            rruleset._rrule[-1]._count -= 1
        return rruleset

    def setrruleset(self, rruleset):
        dtstart = self.dtstart[0].value
        isDate = datetime.date == type(dtstart)
        if isDate:
            dtstart = datetime.datetime(dtstart.year,dtstart.month, dtstart.day)
            untilSerialize = dateToString
        else:
            # make sure to convert time zones to UTC
            untilSerialize = lambda x: dateTimeToString(x, False)

        for name in DATESANDRULES:
            if hasattr(self.contents, name):
                del self.contents[name]
            setlist = getattr(rruleset, '_' + name)
            if name in DATENAMES:
                setlist = list(setlist) # make a copy of the list
                if name == 'rdate' and dtstart in setlist:
                    setlist.remove(dtstart)
                if isDate:
                    setlist = [dt.date() for dt in setlist]
                if len(setlist) > 0:
                    self.add(name).value = setlist
            elif name in RULENAMES:
                for rule in setlist:
                    buf = StringIO.StringIO()
                    buf.write('FREQ=')
                    buf.write(FREQUENCIES[rule._freq])
                    
                    values = {}
                    
                    if rule._interval != 1:
                        values['INTERVAL'] = [str(rule._interval)]
                    if rule._wkst != 0: # wkst defaults to Monday
                        values['WKST'] = [WEEKDAYS[rule._wkst]]
                    if rule._bysetpos is not None:
                        values['BYSETPOS'] = [str(i) for i in rule._bysetpos]
                    
                    if rule._count is not None:
                        values['COUNT'] = [str(rule._count)]
                    elif rule._until is not None:
                        values['UNTIL'] = [untilSerialize(rule._until)]

                    days = []
                    if (rule._byweekday is not None and (
                                  dateutil.rrule.WEEKLY != rule._freq or 
                                   len(rule._byweekday) != 1 or 
                                rule._dtstart.weekday() != rule._byweekday[0])):
                        # ignore byweekday if freq is WEEKLY and day correlates
                        # with dtstart because it was automatically set by
                        # dateutil
                        days.extend(WEEKDAYS[n] for n in rule._byweekday)    
                        
                    if rule._bynweekday is not None:
                        days.extend(str(n) + WEEKDAYS[day] for day, n in rule._bynweekday)
                        
                    if len(days) > 0:
                        values['BYDAY'] = days 
                                                            
                    if rule._bymonthday is not None and len(rule._bymonthday) > 0:
                        if not (rule._freq <= dateutil.rrule.MONTHLY and
                                len(rule._bymonthday) == 1 and
                                rule._bymonthday[0] == rule._dtstart.day):
                            # ignore bymonthday if it's generated by dateutil
                            values['BYMONTHDAY'] = [str(n) for n in rule._bymonthday]

                    if rule._bymonth is not None and len(rule._bymonth) > 0:
                        if not (rule._freq == dateutil.rrule.YEARLY and
                                len(rule._bymonth) == 1 and
                                rule._bymonth[0] == rule._dtstart.month):
                            # ignore bymonth if it's generated by dateutil
                            values['BYMONTH'] = [str(n) for n in rule._bymonth]

                    if rule._byyearday is not None:
                        values['BYYEARDAY'] = [str(n) for n in rule._byyearday]
                    if rule._byweekno is not None:
                        values['BYWEEKNO'] = [str(n) for n in rule._byweekno]

                    # byhour, byminute, bysecond are always ignored for now

                    
                    for key, paramvals in values.iteritems():
                        buf.write(';')
                        buf.write(key)
                        buf.write('=')
                        buf.write(','.join(paramvals))

                    self.add(name).value = buf.getvalue()


            
    rruleset = property(getrruleset, setrruleset)

    def __setattr__(self, name, value):
        """For convenience, make self.contents directly accessible."""
        if name == 'rruleset':
            self.setrruleset(value)
        else:
            super(RecurringComponent, self).__setattr__(name, value)

class RecurringBehavior(behavior.Behavior):
    """Parent Behavior for components which should be RecurringComponents."""
    hasNative = True
    isComponent = True
    
    @staticmethod
    def transformToNative(obj):
        """Turn a recurring Component into a RecurringComponent."""
        if not obj.isNative:
            object.__setattr__(obj, '__class__', RecurringComponent)
            obj.isNative = True
        return obj
    
    @staticmethod
    def transformFromNative(obj):
        if obj.isNative:
            object.__setattr__(obj, '__class__', Component)
            obj.isNative = False
        return obj
    
    @staticmethod        
    def generateImplicitParameters(obj):
        """Generate a UID if one does not exist.
        
        This is just a dummy implementation, for now.
        
        """
        if len(getattr(obj, 'uid', [])) == 0:
            rand = str(int(random.random() * 100000))
            now = datetime.datetime.now(utc)
            now = dateTimeToString(now)
            host = socket.gethostname()
            obj.add(ContentLine('UID', [], now + '-' + rand + '@' + host))        
            
    
class DateTimeBehavior(behavior.Behavior):
    """Parent Behavior for ContentLines containing one DATE-TIME."""
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """Turn obj.value into a datetime.

        RFC2445 allows times without time zone information, "floating times"
        in some properties.  Mostly, this isn't what you want, but when parsing
        a file, real floating times are noted by setting to 'TRUE' the
        X-VOBJ-FLOATINGTIME-ALLOWED parameter.
        
        If a TZID exists, the X-VOBJ-PRESERVE-TZID parameter will be set to
        'TRUE' so the TZID will be recreated when output.

        """
        if obj.isNative: return obj
        obj.isNative = True
        obj.value=str(obj.value)
        #we're cheating a little here, parseDtstart allows DATE
        obj.value=parseDtstart(obj)
        if obj.value.tzinfo is None:
            obj.params['X-VOBJ-FLOATINGTIME-ALLOWED'] = ['TRUE']
        if obj.params.get('TZID'):
            del obj.params['TZID']
        return obj

    @staticmethod
    def transformFromNative(obj, preserveTZ = True):
        """Replace the datetime in obj.value with an ISO 8601 string."""
        if obj.isNative:
            obj.isNative = False
            tzid = TimezoneComponent.pickTzid(obj.value.tzinfo)
            TimezoneComponent.registerTzinfo(obj.value.tzinfo)
            obj.value = dateTimeToString(obj.value, preserveTZ)
            if preserveTZ and tzid is not None:
                obj.params['TZID'] = [tzid]

        return obj

class UTCDateTimeBehavior(DateTimeBehavior):
    """A value which must be specified in UTC."""

    @staticmethod
    def transformFromNative(obj):
        DateTimeBehavior.transformFromNative(obj, False)

class DateOrDateTimeBehavior(behavior.Behavior):
    """Parent Behavior for ContentLines containing one DATE or DATE-TIME."""
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """Turn obj.value into a date or datetime."""
        if obj.isNative: return obj
        obj.isNative = True
        if obj.value == '': return obj
        obj.value=str(obj.value)
        obj.value=parseDtstart(obj)
        if obj.params.get("VALUE", ["DATE-TIME"])[0] == 'DATE-TIME':
            if obj.params.has_key('TZID'): del obj.params['TZID']
        return obj

    @staticmethod
    def transformFromNative(obj):
        """Replace the date or datetime in obj.value with an ISO 8601 string."""
        if type(obj.value) == datetime.date:
            obj.isNative = False
            obj.params['VALUE']=['DATE']
            obj.value = dateToString(obj.value)
            return obj
        else: return DateTimeBehavior.transformFromNative(obj)

class MultiDateBehavior(behavior.Behavior):
    """
    Parent Behavior for ContentLines containing one or more DATE, DATE-TIME, or
    PERIOD.
    
    """
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """
        Turn obj.value into a list of dates, datetimes, or
        (datetime, timedelta) tuples.
        
        """
        if obj.isNative:
            return obj
        obj.isNative = True
        if obj.value == '':
            obj.value = []
            return obj
        tzinfo = getTzid(obj.params.get("TZID", ["UTC"])[0])
        valueParam = obj.params.get("VALUE", ["DATE-TIME"])[0]
        valTexts = obj.value.split(",")
        if valueParam.upper() == "DATE":
            obj.value = [stringToDate(x) for x in valTexts]
        elif valueParam.upper() == "DATE-TIME":
            obj.value = [stringToDateTime(x, tzinfo) for x in valTexts]
        elif valueParam.upper() == "PERIOD":
            obj.value = [stringToPeriod(x, tzinfo) for x in valTexts]
        return obj

    @staticmethod
    def transformFromNative(obj):
        """
        Replace the date, datetime or period tuples in obj.value with
        appropriate strings.
        
        """
        if type(obj.value) == datetime.date:
            obj.isNative = False
            obj.params['VALUE']=['DATE']
            obj.value = ','.join([dateToString(val) for val in obj.value])
            return obj
        else:
            if obj.isNative:
                obj.isNative = False
                transformed = []
                tzid = None
                for val in obj.value:
                    if tzid is None and type(val) == datetime.datetime:
                        tzid = TimezoneComponent.pickTzid(val.tzinfo)
                        if tzid is not None:
                            obj.params['TZID'] = [tzid]
                            TimezoneComponent.registerTzinfo(val.tzinfo)
                    transformed.append(dateTimeToString(val))
                obj.value = ','.join(transformed)
            return obj

class TextBehavior(behavior.Behavior):
    """Provide backslash escape encoding/decoding for single valued properties.
    
    TextBehavior also deals with base64 encoding if the ENCODING parameter is
    explicitly set to BASE64.
    
    """
    base64string = 'BASE64' # vCard uses B
    
    @classmethod
    def decode(cls, line):
        """Remove backslash escaping from line.value."""
        if line.encoded:
            encoding = line.params.get('ENCODING')
            if encoding and encoding[0].upper() == cls.base64string:
                line.value = line.value.decode('base64')
            else:
                line.value = stringToTextValues(line.value)[0]
            line.encoded=False
    
    @classmethod
    def encode(cls, line):
        """Backslash escape line.value."""
        if not line.encoded:
            encoding = line.params.get('ENCODING')
            if encoding and encoding[0].upper() == cls.base64string:
                line.value = line.value.encode('base64').replace('\n', '')
            else:
                line.value = backslashEscape(line.value)
            line.encoded=True

class MultiTextBehavior(behavior.Behavior):
    """Provide backslash escape encoding/decoding of each of several values.
    
    After transformation, value is a list of strings.
    
    """

    @staticmethod
    def decode(line):
        """Remove backslash escaping from line.value, then split on commas."""
        if line.encoded:
            line.value = stringToTextValues(line.value)
            line.encoded=False
    
    @staticmethod
    def encode(line):
        """Backslash escape line.value."""
        if not line.encoded:
            line.value = ','.join(backslashEscape(val) for val in line.value)
            line.encoded=True
    

#------------------------ Registered Behavior subclasses -----------------------
class VCalendar2_0(behavior.Behavior):
    """vCalendar 2.0 behavior."""
    name = 'VCALENDAR'
    description = 'vCalendar 2.0, also known as iCalendar.'
    versionString = '2.0'
    isComponent = True
    sortFirst = ('version', 'calscale', 'method', 'prodid', 'vtimezone')
    knownChildren = {'CALSCALE':  (0, 1, None),#min, max, behaviorRegistry id
                     'METHOD':    (0, 1, None),
                     'VERSION':   (0, 1, None),#required, but auto-generated
                     'PRODID':    (1, 1, None),
                     'VTIMEZONE': (0, None, None),
                     'VEVENT':    (0, None, None),
                     'VTODO':     (0, None, None),
                     'VJOURNAL':  (0, None, None),
                     'VFREEBUSY': (0, None, None)
                    }
                    
    @classmethod
    def generateImplicitParameters(cls, obj):
        """Create PRODID, VERSION, and VTIMEZONEs if needed.
        
        VTIMEZONEs will need to exist whenever TZID parameters exist or when
        datetimes with tzinfo exist.
        
        """
        if len(getattr(obj, 'prodid', [])) == 0:
            obj.add(ContentLine('PRODID', [], PRODID))
        if len(getattr(obj, 'version', [])) == 0:
            obj.add(ContentLine('VERSION', [], cls.versionString))
        tzidsUsed = {}

        def findTzids(obj, table):
            if isinstance(obj, ContentLine):
                if obj.params.get('TZID'):
                    table[obj.params.get('TZID')[0]] = 1
                else:
                    if type(obj.value) == list:
                        for item in obj.value:
                            tzinfo = getattr(obj.value, 'tzinfo', None)
                            tzid = TimezoneComponent.pickTzid(tzinfo)
                            TimezoneComponent.registerTzinfo(tzinfo)
                            if tzid:
                                table[tzid] = 1
                    else:
                        tzinfo = getattr(obj.value, 'tzinfo', None)
                        tzid = TimezoneComponent.pickTzid(tzinfo)
                        TimezoneComponent.registerTzinfo(tzinfo)
                        if tzid:
                            table[tzid] = 1
            for child in obj.getChildren():
                if obj.name is not 'VTIMEZONE':
                    findTzids(child, table)
        
        findTzids(obj, tzidsUsed)
        oldtzids = [x.tzid[0].value for x in getattr(obj, 'vtimezone', [])]
        for tzid in tzidsUsed.keys():
            if tzid in oldtzids or tzid == 'UTC': continue
            obj.add(TimezoneComponent(tzinfo=getTzid(tzid)))
registerBehavior(VCalendar2_0, default=True)

class VTimezone(behavior.Behavior):
    """Timezone behavior."""
    name = 'VTIMEZONE'
    hasNative = True
    isComponent = True
    description = 'A grouping of component properties that defines a time zone.'
    sortFirst = ('tzid', 'last-modified', 'tzurl', 'standard', 'daylight')
    knownChildren = {'TZID':         (1, 1, None),#min, max, behaviorRegistry id
                     'LAST-MODIFIED':(0, 1, None),
                     'TZURL':        (0, 1, None),
                     'STANDARD':     (0, None, None),#NOTE: One of Standard or
                     'DAYLIGHT':     (0, None, None) #      Daylight must appear
                    }

    @classmethod
    def validate(cls, obj, raiseException, *args):
        return True #TODO: FIXME
        if obj.contents.has_key('standard') or obj.contents.has_key('daylight'):
            return super(VTimezone, cls).validate(obj, raiseException, *args)
        else:
            if raiseException:
                m = "VTIMEZONE components must contain a STANDARD or a DAYLIGHT\
                     component"
                raise ValidateError(m)
            return False


    @staticmethod
    def transformToNative(obj):
        if not obj.isNative:
            object.__setattr__(obj, '__class__', TimezoneComponent)
            obj.isNative = True
            obj.registerTzinfo(obj.tzinfo)
        return obj

    @staticmethod
    def transformFromNative(obj):
##        if obj.isNative:
##            object.__setattr__(obj, '__class__', Component)
##            obj.isNative = False
        return obj

        
registerBehavior(VTimezone)

class DaylightOrStandard(behavior.Behavior):
    hasNative = False
    isComponent = True
    knownChildren = {'DTSTART':      (1, 1, None)}#min, max, behaviorRegistry id

registerBehavior(DaylightOrStandard, 'STANDARD')
registerBehavior(DaylightOrStandard, 'DAYLIGHT')


class VEvent(RecurringBehavior):
    """Event behavior."""
    name='VEVENT'
    sortFirst = ('uid', 'recurrence-id', 'dtstart', 'duration', 'dtend')

    description='A grouping of component properties, and possibly including \
                 "VALARM" calendar components, that represents a scheduled \
                 amount of time on a calendar.'
    knownChildren = {'DTSTART':      (0, 1, None),#min, max, behaviorRegistry id
                     'CLASS':        (0, 1, None),  
                     'CREATED':      (0, 1, None),
                     'DESCRIPTION':  (0, 1, None),  
                     'GEO':          (0, 1, None),  
                     'LAST-MODIFIED':(0, 1, None),
                     'LOCATION':     (0, 1, None),  
                     'ORGANIZER':    (0, 1, None),  
                     'PRIORITY':     (0, 1, None),  
                     'DTSTAMP':      (0, 1, None),
                     'SEQUENCE':     (0, 1, None),  
                     'STATUS':       (0, 1, None),  
                     'SUMMARY':      (0, 1, None),                     
                     'TRANSP':       (0, 1, None),  
                     'UID':          (0, 1, None),  
                     'URL':          (0, 1, None),  
                     'RECURRENCE-ID':(0, 1, None),  
                     'DTEND':        (0, 1, None), #NOTE: Only one of DtEnd or
                     'DURATION':     (0, 1, None), #      Duration can appear
                     'ATTACH':       (0, None, None),
                     'ATTENDEE':     (0, None, None),
                     'CATEGORIES':   (0, None, None),
                     'COMMENT':      (0, None, None),
                     'CONTACT':      (0, None, None),
                     'EXDATE':       (0, None, None),
                     'EXRULE':       (0, None, None),
                     'REQUEST-STATUS': (0, None, None),
                     'RELATED-TO':   (0, None, None),
                     'RESOURCES':    (0, None, None),
                     'RDATE':        (0, None, None),
                     'RRULE':        (0, None, None),
                     'VALARM':       (0, None, None)
                    }

    @classmethod
    def validate(cls, obj, raiseException, *args):
        if obj.contents.has_key('DTEND') and obj.contents.has_key('DURATION'):
            if raiseException:
                m = "VEVENT components cannot contain both DTEND and DURATION\
                     components"
                raise ValidateError(m)
            return False
        else:
            return super(VEvent, cls).validate(obj, raiseException, *args)
      
registerBehavior(VEvent)


class VTodo(RecurringBehavior):
    """To-do behavior."""
    name='VTODO'
    description='A grouping of component properties and possibly "VALARM" \
                 calendar components that represent an action-item or \
                 assignment.'
    knownChildren = {'DTSTART':      (0, 1, None),#min, max, behaviorRegistry id
                     'CLASS':        (0, 1, None),
                     'COMPLETED':    (0, 1, None),
                     'CREATED':      (0, 1, None),
                     'DESCRIPTION':  (0, 1, None),  
                     'GEO':          (0, 1, None),  
                     'LAST-MODIFIED':(0, 1, None),
                     'LOCATION':     (0, 1, None),  
                     'ORGANIZER':    (0, 1, None),  
                     'PERCENT':      (0, 1, None),  
                     'PRIORITY':     (0, 1, None),  
                     'DTSTAMP':      (0, 1, None),
                     'SEQUENCE':     (0, 1, None),  
                     'STATUS':       (0, 1, None),  
                     'SUMMARY':      (0, 1, None),
                     'UID':          (0, 1, None),  
                     'URL':          (0, 1, None),  
                     'RECURRENCE-ID':(0, 1, None),  
                     'DUE':          (0, 1, None), #NOTE: Only one of Due or
                     'DURATION':     (0, 1, None), #      Duration can appear
                     'ATTACH':       (0, None, None),
                     'ATTENDEE':     (0, None, None),
                     'CATEGORIES':   (0, None, None),
                     'COMMENT':      (0, None, None),
                     'CONTACT':      (0, None, None),
                     'EXDATE':       (0, None, None),
                     'EXRULE':       (0, None, None),
                     'REQUEST-STATUS': (0, None, None),
                     'RELATED-TO':   (0, None, None),
                     'RESOURCES':    (0, None, None),
                     'RDATE':        (0, None, None),
                     'RRULE':        (0, None, None),
                     'VALARM':       (0, None, None)
                    }

    @classmethod
    def validate(cls, obj, raiseException, *args):
        if obj.contents.has_key('DUE') and obj.contents.has_key('DURATION'):
            if raiseException:
                m = "VTODO components cannot contain both DUE and DURATION\
                     components"
                raise ValidateError(m)
            return False
        else:
            return super(VTodo, cls).validate(obj, raiseException, *args)
      
registerBehavior(VTodo)


class VJournal(RecurringBehavior):
    """Journal entry behavior."""
    name='VJOURNAL'
    knownChildren = {'DTSTART':      (0, 1, None),#min, max, behaviorRegistry id
                     'CLASS':        (0, 1, None),  
                     'CREATED':      (0, 1, None),
                     'DESCRIPTION':  (0, 1, None),  
                     'LAST-MODIFIED':(0, 1, None),
                     'ORGANIZER':    (0, 1, None),  
                     'DTSTAMP':      (0, 1, None),
                     'SEQUENCE':     (0, 1, None),  
                     'STATUS':       (0, 1, None),  
                     'SUMMARY':      (0, 1, None),                     
                     'UID':          (0, 1, None),  
                     'URL':          (0, 1, None),  
                     'RECURRENCE-ID':(0, 1, None),  
                     'ATTACH':       (0, None, None),
                     'ATTENDEE':     (0, None, None),
                     'CATEGORIES':   (0, None, None),
                     'COMMENT':      (0, None, None),
                     'CONTACT':      (0, None, None),
                     'EXDATE':       (0, None, None),
                     'EXRULE':       (0, None, None),
                     'REQUEST-STATUS': (0, None, None),
                     'RELATED-TO':   (0, None, None),
                     'RDATE':        (0, None, None),
                     'RRULE':        (0, None, None)
                    }
registerBehavior(VJournal)


class VFreeBusy(behavior.Behavior):
    """Free/busy state behavior."""
    name='VFREEBUSY'
    description='A grouping of component properties that describe either a \
                 request for free/busy time, describe a response to a request \
                 for free/busy time or describe a published set of busy time.'
    knownChildren = {'DTSTART':      (0, 1, None),#min, max, behaviorRegistry id
                     'CONTACT':      (0, 1, None),
                     'DTEND':        (0, 1, None),
                     'DURATION':     (0, 1, None),
                     'ORGANIZER':    (0, 1, None),  
                     'DTSTAMP':      (0, 1, None), 
                     'UID':          (0, 1, None),  
                     'URL':          (0, 1, None),   
                     'ATTENDEE':     (0, None, None),
                     'COMMENT':      (0, None, None),
                     'FREEBUSY':     (0, None, None),
                     'REQUEST-STATUS': (0, None, None)
                    }
registerBehavior(VFreeBusy)


class VAlarm(behavior.Behavior):
    """Alarm behavior."""
    name='VALARM'
    isComponent = True
    description='Alarms describe when and how to provide alerts about events \
                 and to-dos.'
    knownChildren = {'ACTION':       (1, 1, None),#min, max, behaviorRegistry id
                     'TRIGGER':      (1, 1, None),  
                     'DURATION':     (0, 1, None),
                     'REPEAT':       (0, 1, None),
                     'DESCRIPTION':  (0, 1, None)
                    }

    @staticmethod
    def generateImplicitParameters(obj):
        """Create default ACTION and TRIGGER if they're not set."""
        try:
            obj.action
        except AttributeError:
            obj.add('action').value = 'AUDIO'
        try:
            obj.trigger
        except AttributeError:
            obj.add('trigger').value = datetime.timedelta(0)


    @classmethod
    def validate(cls, obj, raiseException, *args):
        """
        #TODO
     audioprop  = 2*(

                ; 'action' and 'trigger' are both REQUIRED,
                ; but MUST NOT occur more than once

                action / trigger /

                ; 'duration' and 'repeat' are both optional,
                ; and MUST NOT occur more than once each,
                ; but if one occurs, so MUST the other

                duration / repeat /

                ; the following is optional,
                ; but MUST NOT occur more than once

                attach /

     dispprop   = 3*(

                ; the following are all REQUIRED,
                ; but MUST NOT occur more than once

                action / description / trigger /

                ; 'duration' and 'repeat' are both optional,
                ; and MUST NOT occur more than once each,
                ; but if one occurs, so MUST the other

                duration / repeat /

     emailprop  = 5*(

                ; the following are all REQUIRED,
                ; but MUST NOT occur more than once

                action / description / trigger / summary

                ; the following is REQUIRED,
                ; and MAY occur more than once

                attendee /

                ; 'duration' and 'repeat' are both optional,
                ; and MUST NOT occur more than once each,
                ; but if one occurs, so MUST the other

                duration / repeat /

     procprop   = 3*(

                ; the following are all REQUIRED,
                ; but MUST NOT occur more than once

                action / attach / trigger /

                ; 'duration' and 'repeat' are both optional,
                ; and MUST NOT occur more than once each,
                ; but if one occurs, so MUST the other

                duration / repeat /

                ; 'description' is optional,
                ; and MUST NOT occur more than once

                description /
        if obj.contents.has_key('DTEND') and obj.contents.has_key('DURATION'):
            if raiseException:
                m = "VEVENT components cannot contain both DTEND and DURATION\
                     components"
                raise ValidateError(m)
            return False
        else:
            return super(VEvent, cls).validate(obj, raiseException, *args)
        """
        return True
    
registerBehavior(VAlarm)

class Duration(behavior.Behavior):
    """Behavior for Duration ContentLines.  Transform to datetime.timedelta."""
    name = 'DURATION'
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """Turn obj.value into a datetime.timedelta."""
        if obj.isNative: return obj
        obj.isNative = True
        obj.value=str(obj.value)
        if obj.value == '':
            return obj
        else:
            deltalist=stringToDurations(obj.value)
            #When can DURATION have multiple durations?  For now:
            if len(deltalist) == 1:
                obj.value = deltalist[0]
                return obj
            else:
                raise VObjectError("DURATION must have a single duration string.")

    @staticmethod
    def transformFromNative(obj):
        """Replace the datetime.timedelta in obj.value with an RFC2445 string.
        """
        if not obj.isNative: return obj
        obj.isNative = False
        obj.value = timedeltaToString(obj.value)
        return obj
    
registerBehavior(Duration)

class Trigger(behavior.Behavior):
    """DATE-TIME or DURATION"""
    name='TRIGGER'
    description='This property specifies when an alarm will trigger.'
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """Turn obj.value into a timedelta or datetime."""
        value = obj.params.get("VALUE", ["DURATION"])[0]
        try:
            del obj.params['VALUE']
        except KeyError:
            pass
        if obj.value == '':
            obj.isNative = True
            return obj
        elif value  == 'DURATION':
            return Duration.transformToNative(obj)
        elif value == 'DATE-TIME':
            #TRIGGERs with DATE-TIME values must be in UTC, we could validate
            #that fact, for now we take it on faith.
            return DateTimeBehavior.transformToNative(obj)
        else:
            raise NativeError("VALUE must be DURATION or DATE-TIME")        

    @staticmethod
    def transformFromNative(obj):
        if type(obj.value) == datetime.datetime:
            obj.params['VALUE']=['DATE-TIME']
            return DateTimeBehavior.transformFromNative(obj)
        elif type(obj.value) == datetime.timedelta:
            return Duration.transformFromNative(obj)
        else:
            raise NativeError("Native TRIGGER values must be timedelta or datetime")

registerBehavior(Trigger)

class FreeBusy(behavior.Behavior):
    """Free or busy period of time."""
    name='FREEBUSY'
    pass#TODO



#------------------------ Registration of common classes -----------------------

utcDateTimeList = ['LAST-MODIFIED', 'CREATED', 'COMPLETED', 'DTSTAMP']
map(lambda x: registerBehavior(UTCDateTimeBehavior, x),utcDateTimeList)

dateTimeOrDateList = ['DTEND', 'DTSTART', 'DUE', 'RECURRENCE-ID']
map(lambda x: registerBehavior(DateOrDateTimeBehavior, x),
    dateTimeOrDateList)
    
registerBehavior(MultiDateBehavior, 'RDATE')
registerBehavior(MultiDateBehavior, 'EXDATE')

textList = ['CALSCALE', 'METHOD', 'PRODID', 'CLASS', 'COMMENT', 'DESCRIPTION',
            'LOCATION', 'STATUS', 'SUMMARY', 'TRANSP', 'CONTACT', 'RELATED-TO',
            'UID', 'ACTION', 'REQUEST-STATUS', 'TZID']
map(lambda x: registerBehavior(TextBehavior, x), textList)

multiTextList = ['CATEGORIES', 'RESOURCES']
map(lambda x: registerBehavior(MultiTextBehavior, x), multiTextList)

#------------------------ Serializing helper functions -------------------------

def numToDigits(num, places):
    """Helper, for converting numbers to textual digits."""
    s = str(num)
    if len(s) < places:
        return ("0" * (places - len(s))) + s
    elif len(s) > places:
        return s[len(s)-places: ]
    else:
        return s

def timedeltaToString(delta):
    """Convert timedelta to an rfc2445 DURATION."""
    if delta.days == 0: sign = 1
    else: sign = delta.days / abs(delta.days)
    delta = abs(delta)
    days = delta.days
    hours = delta.seconds / 3600
    minutes = (delta.seconds % 3600) / 60
    seconds = delta.seconds % 60
    out = ''
    if sign == -1: out = '-'
    out += 'P'
    if days: out += str(days) + 'D'
    if hours or minutes or seconds: out += 'T'
    elif not days: #Deal with zero duration
        out += '0S'
    if hours: out += str(hours) + 'H'
    if minutes: out += str(minutes) + 'M'
    if seconds: out += str(seconds) + 'S'
    return out

def dateToString(date):
    year  = numToDigits( date.year,  4 )
    month = numToDigits( date.month, 2 )
    day   = numToDigits( date.day,   2 )
    return year + month + day

def dateTimeToString(dateTime, preserveTZ=True):
    """Convert to UTC if tzinfo is set, unless preserveTZ.  Output string."""
    if dateTime.tzinfo and not preserveTZ:
        dateTime = dateTime.astimezone(utc)
    if dateTime.tzinfo == utc: utcString = "Z"
    else: utcString = ""

    year  = numToDigits( dateTime.year,  4 )
    month = numToDigits( dateTime.month, 2 )
    day   = numToDigits( dateTime.day,   2 )
    hour  = numToDigits( dateTime.hour,  2 )
    mins  = numToDigits( dateTime.minute,  2 )
    secs  = numToDigits( dateTime.second,  2 )

    return year + month + day + "T" + hour + mins + secs + utcString

def deltaToOffset(delta):
    absDelta = abs(delta)
    hours = absDelta.seconds / 3600
    hoursString      = numToDigits(hours, 2)
    minutesString    = '00'
    if absDelta == delta:
        signString = "+"
    else:
        signString = "-"
    return signString + hoursString + minutesString


#----------------------- Parsing functions -------------------------------------

def isDuration(s):
    s = string.upper(s)
    return (string.find(s, "P") != -1) and (string.find(s, "P") < 2)

def stringToDate(s, tzinfos=None):
    if tzinfos != None: print "Didn't expect a tzinfos here"
    year  = int( s[0:4] )
    month = int( s[4:6] )
    day   = int( s[6:8] )
    return datetime.date(year,month,day)

def stringToDateTime(s, tzinfo=None):
    """Returns datetime.datetime object."""
    try:
        year   = int( s[0:4] )
        month  = int( s[4:6] )
        day    = int( s[6:8] )
        hour   = int( s[9:11] )
        minute = int( s[11:13] )
        second = int( s[13:15] )
        if len(s) > 15:
            if s[15] == 'Z':
                tzinfo = utc
    except:
        raise ParseError("%s is not a valid DATE-TIME" % s)
    return datetime.datetime(year, month, day, hour, minute, second, 0, tzinfo)


escapableCharList = "\\;,Nn"

def stringToTextValues(s, strict=False):
    """Returns list of strings."""

    def escapableChar (c):
        return c in escapableCharList

    def error(msg):
        if strict:
            raise ParseError(msg)
        else:
            #logger.error(msg)
            print msg

    #vars which control state machine
    charIterator = enumerate(s)
    state        = "read normal"

    current = ""
    results = []

    while True:
        try:
            charIndex, char = charIterator.next()
        except:
            char = "eof"

        if state == "read normal":
            if char == '\\':
                state = "read escaped char"
            elif char == ',':
                state = "read normal"
                results.append(current)
                current = ""
            elif char == "eof":
                state = "end"
            else:
                state = "read normal"
                current = current + char

        elif state == "read escaped char":
            if escapableChar(char):
                state = "read normal"
                if char in 'nN': 
                    current = current + '\n'
                else:
                    current = current + char
            else:
                state = "read normal"
                current = current + char #this is an error, but whatever

        elif state == "end":    #an end state
            if current != "" or len(results) == 0:
                results.append(current)
            return results

        elif state == "error":  #an end state
            return results

        else:
            state = "error"
            error("error: unknown state: '%s' reached in %s" % (state, s))

def stringToDurations(s, strict=False):
    """Returns list of timedelta objects."""
    def makeTimedelta(sign, week, day, hour, minute, sec):
        if sign == "-": sign = -1
        else: sign = 1
        week      = int(week)
        day       = int(day)
        hour      = int(hour)
        minute    = int(minute)
        sec       = int(sec)
        return sign * datetime.timedelta(weeks=week, days=day, hours=hour, minutes=minute, seconds=sec)

    def error(msg):
        if strict:
            raise ParseError(msg)
        else:
            raise ParseError(msg)
            #logger.error(msg)
    
    #vars which control state machine
    charIterator = enumerate(s)
    state        = "start"

    durations = []
    current   = ""
    sign      = None
    week      = 0
    day       = 0
    hour      = 0
    minute    = 0
    sec       = 0

    while True:
        try:
            charIndex, char = charIterator.next()
        except:
            charIndex += 1
            char = "eof"

        if state == "start":
            if char == '+':
                state = "start"
                sign = char
            elif char == '-':
                state = "start"
                sign = char
            elif char.upper() == 'P':
                state = "read field"
            elif char == "eof":
                state = "error"
                error("got end-of-line while reading in duration: " + s)
            elif char in string.digits:
                state = "read field"
                current = current + char   #update this part when updating "read field"
            else:
                state = "error"
                print "got unexpected character %s reading in duration: %s" % (char, s)
                error("got unexpected character %s reading in duration: %s" % (char, s))

        elif state == "read field":
            if (char in string.digits):
                state = "read field"
                current = current + char   #update part above when updating "read field"   
            elif char.upper() == 'T':
                state = "read field"
            elif char.upper() == 'W':
                state = "read field"
                week    = current
                current = ""
            elif char.upper() == 'D':
                state = "read field"
                day     = current
                current = ""
            elif char.upper() == 'H':
                state = "read field"
                hour    = current
                current = ""
            elif char.upper() == 'M':
                state = "read field"
                minute  = current
                current = ""
            elif char.upper() == 'S':
                state = "read field"
                sec     = current
                current = ""
            elif char == ",":
                state = "start"
                durations.append( makeTimedelta(sign, week, day, hour, minute, sec) )
                current   = ""
                sign      = None
                week      = None
                day       = None
                hour      = None
                minute    = None
                sec       = None  
            elif char == "eof":
                state = "end"
            else:
                state = "error"
                error("got unexpected character reading in duration: " + s)
            
        elif state == "end":    #an end state
            #print "stuff: %s, durations: %s" % ([current, sign, week, day, hour, minute, sec], durations)

            if (sign or week or day or hour or minute or sec):
                durations.append( makeTimedelta(sign, week, day, hour, minute, sec) )
            return durations

        elif state == "error":  #an end state
            error("in error state")
            return durations

        else:
            state = "error"
            error("error: unknown state: '%s' reached in %s" % (state, line))

def parseDtstart(contentline):
    tzinfo = getTzid(contentline.params.get("TZID", [None])[0])
    valueParam = contentline.params.get("VALUE", ["DATE-TIME"])[0]
    if valueParam.upper() == "DATE":
        return stringToDate(contentline.value)
    elif valueParam.upper() == "DATE-TIME":
        return stringToDateTime(contentline.value, tzinfo)


def stringToPeriod(s, tzinfo=None):
    values   = string.split(s, "/")
    start = stringToDateTime(values[0], tzinfo)
    valEnd   = values[1]
    if isDuration(valEnd): #period-start = date-time "/" dur-value
        delta = stringToDurations(valEnd)[0]
        return (start, delta)
    else:
        return (start, stringToDateTime(valEnd, tzinfo) - start)

#------------------- Testing and running functions -----------------------------
if __name__ == '__main__':
    import tests
    tests._test()