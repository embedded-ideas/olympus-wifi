import datetime, os, sys, tkinter, time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass

import requests # on Ubuntu install with "apt install -y python3-requests"


###############################################################################
# Class OlympusCamera communicates with an Olympus camera via wifi. It needs  #
# to run on a computer that is connected to the camera's wifi network. This   #
# class queries the camera's supported commands, capabilities, and the camera #
# model.                                                                      #
###############################################################################

class OlympusCamera:

    # The communication via wifi with an Olympus camera is described here:
    # https://raw.githubusercontent.com/ccrome/olympus-omd-remote-control/master/OPC_Communication_Protocol_EN_1.0a/OPC_Communication_Protocol_EN_1.0a.pdf

    @dataclass
    class CmdDescr:
        method: str     # http-method 'get' or 'post'
        args  : dict    # nested dicts of command's key-value args

    @dataclass
    class FileDescr:
        file_name: str  # e.g. "/DCIM/100OLYMP/P1010042.JPG"
        file_size: int  # in bytes
        date_time: str  # ISO date and time, no timezone

    URL_PREFIX          = "http://192.168.0.10/"
    HEADERS             = { 'Host'      : '192.168.0.10',
                            'User-Agent': 'OI.Share v2' }

    ANY_PARAMETER       = '*'
    EMPTY_PARAMETERS    = { ANY_PARAMETER: None }

    def __init__(self):
        self.versions = {}      # version data
        self.supported = set()  # supported functionality
        self.camera_info = {}   # includes camera model
        self.commands = {       # dict { command: CmdDesc }
            'get_commandlist': self.CmdDescr('get', None)
        }

        response = self.send_command('get_commandlist')
        if response is None:
            return

        # Parse XML command description and populate members variables
        # versions, supported, and commands.
        for elem in ElementTree.fromstring(response.text):
            if elem.tag == 'cgi':
                for http_method in elem:
                    if http_method.tag == 'http_method':
                        self.commands[elem.attrib['name']] = \
                            self.CmdDescr(http_method.attrib['type'],
                                          self.commandlist_cmds(http_method))
            elif elem.tag == 'support':
                self. supported.add(elem.attrib['func'])
            elif 'version' in elem.tag:
                self.versions[elem.tag] = elem.text.strip()

        # Issue get-camera-info command. It returns the camera model.
        self.camera_info = self.xml_query('get_caminfo')

        # Get lists of supported values for writable camera properties.
        self.send_command('switch_cammode', mode='rec')
        self.camprop_name2values = {
            prop['propname'] : prop['enum'].split() for prop in
            self.xml_query('get_camprop', com='desc', propname='desclist')
            if prop['attribute'] == 'getset' and 'enum' in prop
        }

        # Switch to mode 'play'.
        self.send_command('switch_cammode', mode='play')

    # Parse parameters in the XML output of command get_commandlist.
    def commandlist_params(self, parent: ElementTree.Element) -> dict:
        params = {}
        for param in parent:
            if param.tag.startswith('cmd'):
                return { self.ANY_PARAMETER: { param.attrib['name'].strip():
                                                 self.commandlist_params(param)
                                             }
                       }
            else:
                name = param.attrib['name'].strip() if 'name' in param.attrib \
                                                    else self.ANY_PARAMETER
                params[name] = self.commandlist_cmds(param)
        return params if len(params) else self.EMPTY_PARAMETERS

    # Parse commands in the XML output of command get_commandlist.
    def commandlist_cmds(self, parent: ElementTree.Element) -> dict:
        cmds = {}
        for cmd in parent:
            assert cmd.tag.startswith('cmd')
            cmds[cmd.attrib['name'].strip()] = self.commandlist_params(cmd)
        return cmds if cmds else None

    # Send command to camera; return Response object or None.
    def send_command(self, command: str, **args) -> requests.Response:

        # Check command and args against what the camera supports.
        if not self.is_valid_command(command, args):
            return

        url = f'{self.URL_PREFIX}{command}.cgi'
        if self.commands[command].method == 'get':
            response = requests.get(url, headers=self.HEADERS, params=args)
        else:
            assert self.commands[command].method == 'post'
            if 'set_value' in args:
                set_value = args['set_value']
                del args['set_value']
            else:
                print(f"Error in '{command}' with args "
                      f"'{', '.join([y+'='+v for k, v in args.items()])}': "
                      "missing entry 'set_value' for method 'post'.",
                      file=sys.stderr)
                return
            headers = self.HEADERS.copy()
            headers['Content-Type'] = 'text/plain;charset=utf-8'
            xml = '<?xml version="1.0"?>\r\n<set>\r\n' \
                  f'<value>{set_value}</value>\r\n</set>\r\n'
            response = requests.post(url, headers=headers, params=args,
                                     data=xml.encode('utf-8'))

        if response.status_code in [requests.codes.ok, requests.codes.accepted]:
            return response
        else:
            xml = self.xml_response(response)
            if xml is not None and isinstance(xml, dict):
                msg = ', '.join([f'{key}={value}'
                                 for key, value in xml.items()])
            else:
                msg = response.text.replace('\r\n','')
            print(f"Error #{response.status_code} "
                  f"for url '{response.url.replace('%2F','/')}': {msg}.",
                  file=sys.stderr)

    # Check validity of command and arguments.
    def is_valid_command(self, command: str, args: dict) -> bool:

        # Check command.
        if command not in self.commands:
            print(f"Error: command '{command}' not supported; valid commands: "
                  f"{', '.join(list(self.commands))}.", file=sys.stderr)
            return False

        valid_command_arguments = self.commands[command].args

        # Check command arguments.
        wildcard = self.ANY_PARAMETER
        for key, value in args.items():

            if key == 'set_value' and self.commands[command].method == 'post':
                continue

            # No (more) valid arguments?
            if valid_command_arguments is None:
                print(f"Error in {command}: '{key}' in "
                      f"{key}={value} not supported.", file=sys.stderr)
                return False

            # Is key a valid argument?
            if key in valid_command_arguments:
                valid_command_arguments = valid_command_arguments[key]
            elif wildcard in valid_command_arguments:
                valid_command_arguments = valid_command_arguments[wildcard]
            else:
                print(f"Error in {command}: '{key}' in {key}={value} "
                      "not supported; supported: "
                      f"{', '.join(list(valid_command_arguments))}.",
                      file=sys.stderr)
                return False

            # Is value valid for key?
            if value in valid_command_arguments:
                valid_command_arguments = valid_command_arguments[value]
            elif wildcard in valid_command_arguments:
                valid_command_arguments = valid_command_arguments[wildcard]
            else:
                print(f"Error in {command}: '{value}' in {key}={value} "
                      "not supported; supported: "
                  f"{', '.join([key+'='+v for v in valid_command_arguments])}.",
                      file=sys.stderr)
                return False
        return True

    # Return a dict with version info; obtained from the camera
    # with command 'get_commandlist'.
    def get_versions(self) -> dict:
        return self.versions

    # Return a set of supported funcs; obtained from the camera
    # with command 'get_commandlist'.
    def get_supported(self) -> set:
        return self.supported

    # Return a dict with an entry for 'model', the camera model.
    def get_camera_info(self) -> dict:
        return self.camera_info

    # Return dict of permitted commands. Each command maps to an instance
    # of class CmdDescr which holds the HTTP-method ('get' or 'post') and
    # nested dicts that represent supported command arguments and their values;
    # obtained from the camera with command 'get_commandlist'.
    def get_commands(self) -> dict:
        return self.commands

    # Return dict of camera properties and list of their supported values.
    def get_settable_propnames_and_values(self) -> dict:
        return self.camprop_name2values

    # Get the value of camera property. A dict of all supported property
    # names can be obtained with:
    #   get_commands()['get_camprop'].args['com']['get']['propname']
    def get_camprop(self, propname: str) -> str:
        self.send_command('switch_cammode', mode='rec')
        return self.xml_query('get_camprop', com='get',
                              propname=propname)['value']

    # Set the value of camera property. A dict of all supported property
    # names can be obtained with:
    #   get_settable_propnames_and_values()
    # The list of supported values for property propname can be obtained with:
    #   get_settable_propnames_and_values()[propname]
    def set_camprop(self, propname: str, value: str) -> None:
        if propname in self.camprop_name2values and \
           value not in self.camprop_name2values[propname]:
            all_values = ', '.join([v for v in
                                    self.camprop_name2values[propname]])
            print(f"Error: value '{value}' not supported for camera property "
                  f"'{propname}'; supported values: {all_values}.",
                  file=sys.stderr)
            return
        self.send_command('switch_cammode', mode='rec')
        self.send_command('set_camprop', com='set', propname=propname,
                          set_value=value)

    # Turn an XML response into a dict or a list of dicts.
    def xml_response(self, response: requests.Response):
        if response is not None:
            if 'Content-Type' in response.headers and \
               response.headers['Content-Type'] == 'text/xml':
                xml = ElementTree.fromstring(response.text)
                my_dict = {}
                my_list = self.xml2dict(xml, my_dict)
                if not my_list:
                    return my_dict
                return my_list[0] if len(my_list) == 1 else my_list

    # Resursively traverse XML and return a list of dicts.
    def xml2dict(self, xml: ElementTree.Element, parent: dict) -> list:
        if xml.text and xml.text.strip():
            parent[xml.tag] = xml.text.strip()
            return []
        else:
            results = []
            params = {}
            for elem in xml:
                results += self.xml2dict(elem, params)
            if params:
                results.append(params)
            return results

    # Send a command and return XML response as dict or list of dicts.
    def xml_query(self, command: str, **args):
        return self.xml_response(self.send_command(command, **args))

    # Set the camera clock to this computer's time and timezone.
    def set_clock(self) -> None:
        self.send_command('set_utctimediff', utctime=
                          datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S"),
                          diff=time.strftime("%z"))

    # The camera takes a picture.
    def take_picture(self) -> None:
        self.send_command('switch_cammode', mode='shutter')
        time.sleep(0.5)
        self.send_command('exec_shutter', com='1st2ndpush')
        time.sleep(0.5)
        self.send_command('exec_shutter', com='2nd1strelease')
        self.send_command('switch_cammode', mode='play')

    # Return list of instances of class FileDescr for a given directory
    # and all its subdirectories on the camera memory card.
    def list_images(self, dir: str = '/DCIM') -> list:
        result = self.send_command('get_imglist', DIR=dir)
        if result is None or result.status_code == 404: 
            return []
        images = []
        for line in result.text.split('\r\n'):
            components = line.split(',')
            if len(components) != 6:
                continue
            path = '/'.join(components[:2])
            size, attrib, date, time = [int(cmp) for cmp in components[2:]]
            datetime = f'{1980+(date>>9)}-{(date>>5)&15:02d}-{date&31:02d}'\
                       f'T{time>>11:02d}:{(time>>5)&63:02d}:{2*(time&31):02d}'
            if attrib & 2: # hidden
                print(f"Ignoring hidden file '{path}'.")
                continue
            if attrib & 4: # system
                print(f"Ignoring system file '{path}'.")
                continue
            if attrib & 8: # volume
                print(f"Ignoring volume '{path}'.")
                continue
            if attrib & 16: # directory
                images += self.list_images(path)
            else:
                images.append(self.FileDescr(path, size, datetime))
        return images

    # Returns a jpeg image.
    def download_thumbnail(self, dir: str) -> bytes:
        result = self.send_command('get_thumbnail', DIR=dir)
        if result is not None:
            return result.content

    # Returns full-size jpeg image.
    def download_image(self, dir: str) -> bytes:
        response = requests.get(self.URL_PREFIX + dir[1:], headers=self.HEADERS)
        if response.status_code == requests.codes.ok:
            return response.content

    # Start the liveview; the camera will broadcast an RTP live stream at the
    # given UDP port in the given resolution. Supported values for the
    # resolution can be queried with member function:
    #   get_commands()['switch_cammode'].args['mode']['rec']['lvqty']
    def start_liveview(self, port: int, lvqty: str) -> None:
        self.send_command('switch_cammode', mode='rec', lvqty=lvqty)
        self.send_command('exec_takemisc', com='startliveview', port=port)

    # Stop the liveview; the camera will no longer send the RTP live stream.
    def stop_liveview(self) -> None:
        self.send_command('exec_takemisc', com='stopliveview')