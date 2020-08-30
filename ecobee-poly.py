#!/usr/bin/env python3

CLOUD = False

try:
    import polyinterface
except ImportError:
    import pgc_interface as polyinterface
    CLOUD = True
import sys
import json
import time
import http.client
import urllib.parse
from datetime import datetime
import os
import os.path
import re
import logging
from copy import deepcopy

from pgSession import pgSession
from node_types import Thermostat, Sensor, Weather
from node_funcs import *

LOGGER = polyinterface.LOGGER

ECOBEE_API_URL = 'api.ecobee.com'

class Controller(polyinterface.Controller):
    def __init__(self, polyglot):
        super().__init__(polyglot)
        self.name = 'Ecobee Controller'
        self.tokenData = {}
        self.msgi = {}
        self.in_discover = False
        self.discover_st = False
        self.refreshingTokens = False
        self.pinRun = False
        self.hb = 0
        self._cloud = CLOUD

    def start(self):
        #self.removeNoticesAll()
        LOGGER.info('Started Ecobee v2 NodeServer')
        self.heartbeat()
        self.serverdata = get_server_data(LOGGER)
        LOGGER.info('Ecobee NodeServer Version {}'.format(self.serverdata['version']))
        nsv = 'nodeserver_version'
        ud  = False
        cust_data = self.polyConfig['customData']
        if not nsv in cust_data:
            LOGGER.info("Adding {}={} to customData".format(nsv,self.serverdata['version']))
            cust_data[nsv] = self.serverdata['version']
            ud = True
        elif cust_data[nsv] != self.serverdata['version']:
            LOGGER.info("Update {} from {} to {} in customData".format(nsv,cust_data[nsv],self.serverdata['version']))
            cust_data[nsv] = self.serverdata['version']
            ud = True
        # Delete the saved token data we were doing for a while
        # For PG3 we can start doing it again, but not in PG2 since it costs $$
        # Can't iterate over keys, since the keys get removed, causes python crash
        keys = list(cust_data.keys())
        for key in keys:
            if re.match('tokenData2020.*',key):
                LOGGER.info("Deleting old customData key {}".format(key))
                del cust_data[key]
        if ud:
            self.saveCustomData(cust_data)
        LOGGER.debug("customData=\n"+json.dumps(cust_data,sort_keys=True,indent=2))
        self.removeNoticesAll()
        self.set_debug_mode()
        self.get_session()
        # Force to false, and successful communication will fix it
        #self.set_ecobee_st(False) Causes it to always stay false.
        if 'tokenData' in self.polyConfig['customData']:
            self.tokenData = self.polyConfig['customData']['tokenData']
            if self._checkTokens():
                self.discover()
        else:
            LOGGER.info('No tokenData, will need to authorize...')
            self._getPin()
            self.reportDrivers()

    def get_session(self):
        self.session = pgSession(self,self.name,LOGGER,ECOBEE_API_URL,debug_level=self.debug_level)

    def _checkTokens(self):
        if self.refreshingTokens:
            LOGGER.error('Waiting for token refresh to complete...')
            while self.refreshingTokens:
                time.sleep(.1)
        if 'access_token' in self.tokenData:
            ts_now = datetime.now()
            if 'expires' in self.tokenData:
                ts_exp = datetime.strptime(self.tokenData['expires'], '%Y-%m-%dT%H:%M:%S')
                exp_d  = ts_exp - ts_now
                if exp_d.total_seconds() < int(self.polyConfig['longPoll']) * 2:
                    self.l_info('_checkTokens','Tokens {} expires {} will expire in {} seconds, so refreshing now...'.format(self.tokenData['refresh_token'],self.tokenData['expires'],exp_d.total_seconds()))
                    return self._getRefresh()
                else:
                    # Only print this ones, then once a minute at most...
                    sd = True
                    if 'ctdt' in self.msgi:
                        md = datetime.now() - self.msgi['ctdt']
                        if md.total_seconds() < 60:
                            sd = False
                    if sd:
                        self.l_debug('_checkTokens',0,'Tokens valid until: {} ({} seconds, longPoll={})'.format(self.tokenData['expires'],exp_d.seconds,int(self.polyConfig['longPoll'])))
                    self.msgi['ctdt'] = ts_now
                    self.set_auth_st(True)
                    return True
            else:
                self.l_error('_checkTokens', 'No expires in tokenData:{}'.format(self.tokenData))
        else:
            self.set_auth_st(False)
            self.l_error('_checkTokens','tokenData or access_token not available')
            # self.saveCustomData({})
            # this._getPin()
            return False

    _tname = 'refresh_status'
    def _startRefresh(self,test=False):
        # Someone else already refreshing?
        rval = False
        cdata = deepcopy(self.polyConfig['customData'])
        # See if someone else already refreshed it?  Very small chance of this happening on PGC, but it could.
        if 'tokenData' in cdata:
            if 'refresh_token' in cdata['tokenData']:
                if self.tokenData['refresh_token'] != cdata['tokenData']['refresh_token']:
                    LOGGER.error("Someone already refreshed the token!")
                    LOGGER.error(" Old: {}".format(self.tokenData))
                    LOGGER.error(" New: {}".format(cdata['tokenData']))
                    LOGGER.error("We will use the new tokens...")
                    self.tokenData = deepcopy(cdata['tokenData'])
                    return False
        # Now see if someone is trying to refresh it.
        uparam = cdata.get(self._tname)
        LOGGER.debug('uparam={}'.format(uparam))
        if uparam is None or uparam is False:
            LOGGER.debug('We can do it!')
            rval = True
        else:
            LOGGER.error("Someone is already refreshing at {}...".format(uparam))
            # See if it has expired
            ts_now = datetime.now()
            try:
                ts_start = datetime.strptime(uparam,'%Y-%m-%dT%H:%M:%S')
            except Exception as e:
                LOGGER.error('convert time {} failed {}, will grab the lock'.format(uparam,e))
                rval = True
            else:
                ts_diff  = ts_now - ts_start
                if ts_diff.total_seconds() > 120:
                    LOGGER.error("But their attempt was {} seconds ago, so we will grab the lock...".format(ts_diff.total_seconds()))
                    rval = True
        if rval:
            cdata[self._tname] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            self.saveCustomData(cdata)
            self.refreshingTokens = True
        return rval

    # This is only called when refresh fails, when it works saveTokens clears
    # it, otherwise we get_ a race on who's customData is saved...
    def _endRefresh(self,refresh_data=False,test=False):
        cdata = deepcopy(self.polyConfig['customData'])
        # Old stuff
        if 'pinData' in cdata:
            del cdata['pinData']
        if refresh_data is not False:
            if 'expires_in' in refresh_data:
                ts = time.time() + refresh_data['expires_in']
                refresh_data['expires'] = datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")
            # Save new token data in customData
            cdata['tokenData'] = refresh_data
            # And save in our variable for checking, unless in test mode...
            if not test:
                self.tokenData = deepcopy(refresh_data)
            self.set_auth_st(True)
            self.removeNoticesAll()
        # This says we are clearing the lock...
        cdata[self._tname] = False
        LOGGER.debug("Sending customData=\n"+json.dumps(cdata,sort_keys=True,indent=2))
        self.saveCustomData(cdata)
        LOGGER.debug('cleared lock')
        self.refreshingTokens = False

    # test option is passed in to force a refresh and save to db, but not our
    # locally saved self.tokenData.  This makes it look like someone else
    # refreshed the token, so we just grab from the db.
    def _getRefresh(self,test=False):
        if 'refresh_token' in self.tokenData:
            if not self._startRefresh(test=test):
                return False
            LOGGER.debug('Attempting to refresh tokens...')
            res = self.session.post('token',
                params = {
                    'grant_type':    'refresh_token',
                    'client_id':     self.serverdata['api_key'],
                    'refresh_token': self.tokenData['refresh_token']
                })
            if res is False:
                self.set_ecobee_st(False)
                self._endRefresh(test=test)
                return False
            self.set_ecobee_st(True)
            res_data = res['data']
            res_code = res['code']
            if res_data is False:
                LOGGER.error('No data returned.')
            else:
                # https://www.ecobee.com/home/developer/api/documentation/v1/auth/auth-req-resp.shtml
                if 'error' in res_data:
                    self.addNotice({'grant_error': "{}: {} ".format(res_data['error'], res_data['error_description'])})
                    self.addNotice({'grant_info': "For access_token={} refresh_token={} expires={}".format(self.tokenData['access_token'],self.tokenData['refresh_token'],self.tokenData['expires'])})
                    LOGGER.error('Requesting Auth: {} :: {}'.format(res_data['error'], res_data['error_description']))
                    LOGGER.error('For access_token={} refresh_token={} expires={}'.format(self.tokenData['access_token'],self.tokenData['refresh_token'],self.tokenData['expires']))
                    # JimBo: This can only happen if our refresh_token is bad, so we need to force a re-auth
                    if res_data['error'] == 'invalid_grant':
                        self._reAuth('{}'.format(res_data['error']))
                        self._endRefresh(test=test)
                    return False
                elif 'access_token' in res_data:
                    self._endRefresh(res_data,test=test)
                    return True
        else:
            self._reAuth(' refresh_token not Found in tokenData={}'.format(self.tokenData))
        self._endRefresh(test=test)
        return False

    def _reAuth(self, reason):
        # Need to re-auth!
        LOGGER.error('_reAuth because: {}'.format(reason))
        cdata = deepcopy(self.polyConfig['customData'])
        if not 'tokenData' in cdata:
            LOGGER.error('No tokenData in customData: {}'.format(cdata))
        cdata[self._tname] = False
        self.saveCustomData(cdata)
        self._getPin()


    def _getTokens(self, pinData):
        LOGGER.debug('PIN: {} found. Attempting to get tokens...'.format(pinData['ecobeePin']))
        res = self.session.post('token',
            params = {
                        'grant_type':  'ecobeePin',
                        'client_id':   self.serverdata['api_key'],
                        'code':        pinData['code']
                    })
        if res is False:
            self.set_ecobee_st(False)
            self.set_auth_st(False)
            return False
        res_data = res['data']
        res_code = res['code']
        if res_data is False:
            LOGGER.error('_getTokens: No data returned.')
            self.set_auth_st(False)
            return False
        if 'error' in res_data:
            LOGGER.error('_getTokens: {} :: {}'.format(res_data['error'], res_data['error_description']))
            self.set_auth_st(False)
            return False
        if 'access_token' in res_data:
            LOGGER.debug('Got first set of tokens sucessfully.')
            self._endRefresh(res_data)
            return True
        self.set_auth_st(False)

    def _getPin(self):
        res = self.session_get('authorize',
                              {
                                  'response_type':  'ecobeePin',
                                  'client_id':      self.serverdata['api_key'],
                                  'scope':          'smartWrite'
                              })
        if res is False:
            self.refreshingTokens = False
            return False
        res_data = res['data']
        res_code = res['code']
        if 'ecobeePin' in res_data:
            msg = 'Click <a target="_blank" href="https://www.ecobee.com/home/ecobeeLogin.jsp">here</a> to login to your Ecobee account. Click on Profile > My Apps > Add Application and enter PIN: <b>{}</b>. Then restart the nodeserver. You have 10 minutes to complete this. The NodeServer will check every 60 seconds.'.format(res_data['ecobeePin'])
            LOGGER.info('_getPin: {}'.format(msg))
            self.addNotice({'getPin': msg})
            # cust_data = deepcopy(self.polyConfig['customData'])
            # cust_data['pinData'] = data
            # self.saveCustomData(cust_data)
            waitingOnPin = True
            stime = 30
            while waitingOnPin:
                time.sleep(stime)
                if self._getTokens(res_data):
                    waitingOnPin = False
                    self.discover()
                else:
                    if stime < 180:
                        stime += 30
        else:
            msg = 'ecobeePin Failed code={}: {}'.format(res_code,res_data)
            self.addNotice({'getPin': msg})

    def shortPoll(self):
        pass

    def longPoll(self):
        # Call discovery if it failed on startup
        LOGGER.debug("{}:longPoll".format(self.address))
        self.heartbeat()
        if self.in_discover:
            LOGGER.debug("{}:longPoll: Skipping since discover is still running".format(self.address))
            return
        if self.discover_st is False:
            self.discover()
        self.updateThermostats()

    def heartbeat(self):
        LOGGER.debug('heartbeat hb={}'.format(self.hb))
        if self.hb == 0:
            self.reportCmd("DON",2)
            self.hb = 1
        else:
            self.reportCmd("DOF",2)
            self.hb = 0

    def updateThermostats(self,force=False):
        LOGGER.debug("{}:updateThermostats: start".format(self.address))
        thermostats = self.getThermostats()
        if not isinstance(thermostats, dict):
            LOGGER.error('Thermostats instance wasn\'t dictionary. Skipping...')
            return
        for thermostatId, thermostat in thermostats.items():
            LOGGER.debug("{}:updateThermostats: {}".format(self.address,thermostatId))
            if self.checkRev(thermostat):
                address = self.thermostatIdToAddress(thermostatId)
                if address in self.nodes:
                    LOGGER.debug('Update detected in thermostat {}({}) doing full update.'.format(thermostat['name'], address))
                    fullData = self.getThermostatFull(thermostatId)
                    if fullData is not False:
                        self.nodes[address].update(thermostat, fullData)
                    else:
                        LOGGER.error('Failed to get updated data for thermostat: {}({})'.format(thermostat['name'], thermostatId))
                else:
                    LOGGER.error("Thermostat id '{}' address '{}' is not in our node list. thermostat: {}".format(thermostatId,address,thermostat))
            else:
                LOGGER.info("No {} '{}' update detected".format(thermostatId,thermostat['name']))
        LOGGER.debug("{}:updateThermostats: done".format(self.address))

    def checkRev(self, tstat):
        if tstat['thermostatId'] in self.revData:
            curData = self.revData[tstat['thermostatId']]
            if (tstat['thermostatRev'] != curData['thermostatRev']
                    or tstat['alertsRev'] != curData['alertsRev']
                    or tstat['runtimeRev'] != curData['runtimeRev']
                    or tstat['intervalRev'] != curData['intervalRev']):
                return True
        return False

    def query(self):
        self.reportDrivers()
        for node in self.nodes:
            self.nodes[node].reportDrivers()

    def stop(self):
        LOGGER.debug('NodeServer stoping...')
        self.set_ecobee_st(False)

    def thermostatIdToAddress(self,tid):
        return 't{}'.format(tid)

    def discover(self, *args, **kwargs):
        # True means we are in dsocvery
        if self.in_discover:
            LOGGER.info('Discovering Ecobee Thermostats already running?')
            return True
        self.in_discover = True
        self.discover_st = False
        try:
            self.discover_st = self._discover()
        except Exception as e:
            self.l_error('discover','failed: {}'.format(e),True)
            self.discover_st = False
        self.in_discover = False
        return self.discover_st

    def _discover(self, *args, **kwargs):
        LOGGER.info('Discovering Ecobee Thermostats')
        if not 'access_token' in self.tokenData:
            return False
        self.revData = {} # Intialize in case we fail
        thermostats = self.getThermostats()
        if thermostats is False:
            LOGGER.error("Discover Failed, No thermostats returned!  Will try again on next long poll")
            return False
        self.revData = deepcopy(thermostats)
        #
        # Build or update the profile first.
        #
        self.check_profile(thermostats)
        #
        # Now add our thermostats
        #
        for thermostatId, thermostat in thermostats.items():
            address = self.thermostatIdToAddress(thermostatId)
            if not address in self.nodes:
                fullData = self.getThermostatFull(thermostatId)
                if fullData is not False:
                    tstat = fullData['thermostatList'][0]
                    useCelsius = True if tstat['settings']['useCelsius'] else False
                    self.addNode(Thermostat(self, address, address, thermostatId,
                                            'Ecobee - {}'.format(get_valid_node_name(thermostat['name'])),
                                            thermostat, fullData, useCelsius))
        return True

    def check_profile(self,thermostats):
        self.profile_info = get_profile_info(LOGGER)
        #
        # First get all the climate programs so we can build the profile if necessary
        #
        climates = dict()
        for thermostatId, thermostat in thermostats.items():
            # Only get program data if we have the node.
            fullData = self.getThermostatSelection(thermostatId,includeProgram=True)
            if fullData is not False:
                programs = fullData['thermostatList'][0]['program']
                climates[thermostatId] = list()
                for climate in programs['climates']:
                    climates[thermostatId].append({'name': climate['name'], 'ref':climate['climateRef']})
        LOGGER.debug("check_profile: climates={}".format(climates))
        #
        # Set Default profile version if not Found
        #
        cdata = deepcopy(self.polyConfig['customData'])
        LOGGER.info('check_profile: profile_info={}'.format(self.profile_info))
        LOGGER.info('check_profile:   customData={}'.format(cdata))
        if not 'profile_info' in cdata:
            update_profile = True
        elif self.profile_info['version'] == cdata['profile_info']['version']:
            # Check if the climates are different
            update_profile = False
            LOGGER.info('check_profile: update_profile={} checking climates.'.format(update_profile))
            if 'climates' in cdata:
                current = cdata['climates']
                if not update_profile:
                    # Check if the climates have changed.
                    for id in climates:
                        if id in current:
                            if len(climates[id]) == len(current[id]):
                                for i in range(len(climates[id])):
                                    if climates[id][i] != current[id][i]:
                                        update_profile = True
                            else:
                                update_profile = True
                        else:
                            update_profile = True
            else:
                update_profile = True
        else:
            update_profile = True
        LOGGER.warning('check_profile: update_profile={}'.format(update_profile))
        if update_profile:
            self.write_profile(climates)
            self.poly.installprofile()
            cdata['profile_info'] = self.profile_info
            cdata['climates'] = climates
            self.saveCustomData(cdata)

    def write_profile(self,climates):
      pfx = '{}:write_profile:'.format(self.address)
      #
      # Start the nls with the template data.
      #
      en_us_txt = "profile/nls/en_us.txt"
      make_file_dir(en_us_txt)
      LOGGER.info("{0} Writing {1}".format(pfx,en_us_txt))
      nls_tmpl = open("template/en_us.txt", "r")
      nls      = open(en_us_txt,  "w")
      for line in nls_tmpl:
        nls.write(line)
      nls_tmpl.close()
      # Open the nodedef custom for writing
      nodedef_f = 'profile/nodedef/custom.xml'
      LOGGER.info("{0} Writing {1}".format(pfx,nodedef_f))
      nodedef_h = open(nodedef_f, "w")
      nodedef_h.write('<nodedefs>\n')
      # Open the editor custom for writing
      editor_f = 'profile/editor/custom.xml'
      LOGGER.info("{0} Writing {1}".format(pfx,editor_f))
      editor_h = open(editor_f, "w")
      editor_h.write('<editors>\n')
      for id in climates:
        # Read thermostat template to write the custom version.
        in_h  = open('template/thermostat.xml','r')
        for line in in_h:
            nodedef_h.write(re.sub(r'tstatid',r'{0}'.format(id),line))
        in_h.close()
        # Read the editor template to write the custom version
        in_h  = open('template/editors.xml','r')
        for line in in_h:
            line = re.sub(r'tstatid',r'{0}'.format(id),line)
            line = re.sub(r'tstatcnta',r'{0}'.format(len(climateList)-1),line)
            # This is minus 3 because we don't allow selecting vacation or smartAway, ...
            # But not currently using this because we don't have different list for
            # status and programs?
            line = re.sub(r'tstatcnt',r'{0}'.format(len(climateList)-5),line)
            editor_h.write(line)
        in_h.close()
        # Then the NLS lines.
        nls.write("\n")
        nls.write('ND-EcobeeC_{0}-NAME = Ecobee Thermostat {0} (C)\n'.format(id))
        nls.write('ND-EcobeeC_{0}-ICON = Thermostat\n'.format(id))
        nls.write('ND-EcobeeF_{0}-NAME = Ecobee Thermostat {0} (F)\n'.format(id))
        nls.write('ND-EcobeeF_{0}-ICON = Thermostat\n'.format(id))
        # ucfirst them all
        customList = list()
        for i in range(len(climateList)):
            customList.append(climateList[i][0].upper() + climateList[i][1:])
        # Now see if there are custom names
        for i in range(len(climateList)):
            name = climateList[i]
            # Find this name in the map and replace with our name.
            for cli in climates[id]:
                if cli['ref'] == name:
                    customList[i] = cli['name']
        LOGGER.debug("{} customList={}".format(pfx,customList))
        for i in range(len(customList)):
            nls.write("CT_{}-{} = {}\n".format(id,i,customList[i]))
      nodedef_h.write('</nodedefs>\n')
      nodedef_h.close()
      editor_h.write('</editors>\n')
      editor_h.close()
      nls.close()
      LOGGER.info("{} done".format(pfx))

    # Calls session.get and converts params to weird ecobee formatting.
    def session_get (self,path,data):
        if path == 'authorize':
            # All calls before with have auth token, don't reformat with json
            return self.session.get(path,data)
        else:
            res = self.session.get(path,{ 'json': json.dumps(data) },
                                    auth='{} {}'.format(self.tokenData['token_type'], self.tokenData['access_token'])
                                    )
            if res is False:
                return res
            if res['data'] is False:
                return False
            self.l_debug('session_get', 0, 'res={}'.format(res))
            if not 'status' in res['data']:
                return res
            res_st_code = int(res['data']['status']['code'])
            if res_st_code == 0:
                return res
            LOGGER.error('Checking Bad Status Code {} for {}'.format(res_st_code,res))
            if res_st_code == 14:
                self.l_error('session_get', 'Token has expired, will refresh')
                # TODO: Should this be a loop instead ?
                if self._getRefresh() is True:
                    return self.session.get(path,{ 'json': json.dumps(data) },
                                     auth='{} {}'.format(self.tokenData['token_type'], self.tokenData['access_token']))
            elif res_st_code == 16:
                self._reAuth("session_get: Token deauthorized by user: {}".format(res))
            return False

    def getThermostats(self):
        if not self._checkTokens():
            LOGGER.debug('getThermostat failed. Couldn\'t get tokens.')
            return False
        LOGGER.debug('getThermostats: Getting Summary...')
        res = self.session_get('1/thermostatSummary',
                               {
                                    'selection': {
                                        'selectionType': 'registered',
                                        'selectionMatch': '',
                                        'includesEquipmentStatus': True
                                    },
                                })
        if res is False:
            self.set_ecobee_st(False)
            return False
        self.set_ecobee_st(True)
        thermostats = {}
        res_data = res['data']
        res_code = res['code']
        if res_data is False:
            self.l_error('getThermostats','Ecobee returned code {} but no data? ({})'.format(res_code,res_data))
            return thermostats
        if 'revisionList' in res_data:
            if res_data['revisionList'] is False:
                self.l_error('getThermostats','Ecobee returned code {} but no revisionList? ({})'.format(res_code,res_data['revisionList']))
            for thermostat in res_data['revisionList']:
                revisionArray = thermostat.split(':')
                thermostats['{}'.format(revisionArray[0])] = {
                    'name': revisionArray[1],
                    'thermostatId': revisionArray[0],
                    'connected': revisionArray[2],
                    'thermostatRev': revisionArray[3],
                    'alertsRev': revisionArray[4],
                    'runtimeRev': revisionArray[5],
                    'intervalRev': revisionArray[6]
                }
        return thermostats

    def getThermostatFull(self, id):
        return self.getThermostatSelection(id,True,True,True,True,True,True,True,True,True,True,True,True)

    def getThermostatSelection(self,id,
                               includeEvents=False,
                               includeProgram=False,
                               includeSettings=False,
                               includeRuntime=False,
                               includeExtendedRuntime=False,
                               includeLocation=False,
                               includeEquipmentStatus=False,
                               includeVersion=False,
                               includeUtility=False,
                               includeAlerts=False,
                               includeWeather=False,
                               includeSensors=False
                               ):
        if not self._checkTokens():
            LOGGER.error('getThermostat failed. Couldn\'t get tokens.')
            return False
        LOGGER.info('Getting Thermostat Data for {}'.format(id))
        res = self.session_get('1/thermostat',
                               {
                                   'selection': {
                                       'selectionType': 'thermostats',
                                       'selectionMatch': id,
                                       'includeEvents': includeEvents,
                                       'includeProgram': includeProgram,
                                       'includeSettings': includeSettings,
                                       'includeRuntime': includeRuntime,
                                       'includeExtendedRuntime': includeExtendedRuntime,
                                       'includeLocation': includeLocation,
                                       'includeEquipmentStatus': includeEquipmentStatus,
                                       'includeVersion': includeVersion,
                                       'includeUtility': includeUtility,
                                       'includeAlerts': includeAlerts,
                                       'includeWeather': includeWeather,
                                       'includeSensors': includeSensors
                                       }
                               }
                           )
        self.l_debug('getThermostatSelection',0,'done'.format(id))
        self.l_debug('getThermostatSelection',1,'data={}'.format(res))
        if res is False or res is None:
            return False
        return res['data']

    def ecobeePost(self, thermostatId, postData = {}):
        if not self._checkTokens():
            LOGGER.error('ecobeePost failed. Tokens not available.')
            return False
        LOGGER.info('Posting Update Data for Thermostat {}'.format(thermostatId))
        postData['selection'] = {
            'selectionType': 'thermostats',
            'selectionMatch': thermostatId
        }
        res = self.session.post('1/thermostat',params={'json': 'true'},payload=postData,
            auth='{} {}'.format(self.tokenData['token_type'], self.tokenData['access_token']),dump=True)
        if res is False:
            self.refreshingTokens = False
            self.set_ecobee_st(False)
            return False
        self.set_ecobee_st(True)
        if 'error' in res:
            LOGGER.error('ecobeePost: error="{}" {}'.format(res['error'], res['error_description']))
            return False
        res_data = res['data']
        res_code = res['code']
        if 'status' in res_data:
            if 'code' in res_data['status']:
                if res_data['status']['code'] == 0:
                    return True
                else:
                    LOGGER.error('Bad return code {}:{}'.format(res_data['status']['code'],res_data['status']['message']))
        return False

    def cmd_poll(self,  *args, **kwargs):
        LOGGER.debug("{}:cmd_poll".format(self.address))
        self.updateThermostats(force=True)
        self.query()

    def cmd_query(self, *args, **kwargs):
        LOGGER.debug("{}:cmd_query".format(self.address))
        self.query()

    def cmd_upload_profile(self, *args, **kwargs):
        LOGGER.debug("{}:cmd_upload_profile".format(self.address))
        self.poly.installprofile()

    def cmd_debug_mode(self,command):
        val = int(command.get('value'))
        self.l_info("cmd_debug_mode",val)
        self.set_debug_mode(val)

    def cmd_test(self,  *args, **kwargs):
        LOGGER.debug("{}".format(self.address))
        self._getRefresh(test=True)

    def set_all_logs(self,level):
        self.l_info("set_all_logs",level)
        LOGGER.setLevel(level)
        #logging.getLogger('requests').setLevel(level)
        #logging.getLogger('urllib3').setLevel(level)

    def set_debug_mode(self,level=None):
        self.l_info("set_debug_mode",level)
        if level is None:
            try:
                level = self.getDriver('GV2')
            except:
                self.l_error('set_debug_mode','getDriver(GV2) failed',True)
            if level is None:
                level = 20
        level = int(level)
        self.debug_mode = level
        try:
            self.setDriver('GV2', level)
        except:
            self.l_error('set_debug_mode','setDriver(GV2) failed',True)
        self.debug_level = 0
        if level < 20:
            self.set_all_logs(logging.DEBUG)
            # 9 & 8 incrase pgsession debug level
            if level == 9:
                self.debug_level = 1
            elif level == 8:
                self.debug_level = 2
        elif level <= 20:
            self.set_all_logs(logging.INFO)
        elif level <= 30:
            self.set_all_logs(logging.WARNING)
        elif level <= 40:
            self.set_all_logs(logging.ERROR)
        elif level <= 50:
            self.set_all_logs(logging.CRITICAL)
        else:
            self.l_error("set_debug_mode","Unknown level {0}".format(level))
        self.l_info("set_debug_mode"," session debug_level={}".format(self.debug_level))

    def set_ecobee_st(self,val):
      ival = 1 if val else 0
      LOGGER.debug("{}:set_ecobee_st: {}={}".format(self.address,val,ival))
      self.setDriver('GV1',ival)

    def set_auth_st(self,val):
      ival = 1 if val else 0
      LOGGER.debug("{}:set_auth_st: {}={}".format(self.address,val,ival))
      self.setDriver('GV3',ival)

    def l_info(self, name, string):
        LOGGER.info("%s:%s:%s: %s" %  (self.id,self.name,name,string))

    def l_error(self, name, string, exc_info=False):
        LOGGER.error("%s:%s:%s: %s" % (self.id,self.name,name,string), exc_info=exc_info)

    def l_warning(self, name, string):
        LOGGER.warning("%s:%s:%s: %s" % (self.id,self.name,name,string))

    def l_debug(self, name, level, string, exc_info=False):
        if level <= self.debug_level:
            LOGGER.debug("%s:%s:%s: %s" % (self.id,self.name,name,string), exc_info=exc_info)

    id = 'ECO_CTR'
    commands = {
        'DISCOVER': discover,
        'QUERY': cmd_query,
        'POLL': cmd_poll,
        'DEBUG': cmd_debug_mode,
        'UPLOAD_PROFILE': cmd_upload_profile,
        'TEST': cmd_test,
    }
    drivers = [
        {'driver': 'ST', 'value': 1, 'uom': 2},
        {'driver': 'GV1', 'value': 0, 'uom': 2},
        {'driver': 'GV2', 'value': 30, 'uom': 25},
        {'driver': 'GV3', 'value': 0, 'uom': 2}
    ]

if __name__ == "__main__":
    try:
        polyglot = polyinterface.Interface('Ecobee')
        polyglot.start()
        control = Controller(polyglot)
        control.runForever()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
