#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import requests
import os
import sys

GET_STATUS_API = 'api/status'
GET_INDEX_PATTERN_INFO_URI = 'api/saved_objects/_find'
GET_FIELDS_URI = 'api/index_patterns/_fields_for_wildcard'
PUT_INDEX_PATTERN_URI = 'api/saved_objects/index-pattern'
OS_GET_TEMPLATE_URI = '_template'

###################################################################################################
debug = False
scriptName = os.path.basename(__file__)
scriptPath = os.path.dirname(os.path.realpath(__file__))
origPath = os.getcwd()

###################################################################################################
# print to stderr
def eprint(*args, **kwargs):
  print(*args, file=sys.stderr, **kwargs)

###################################################################################################
# convenient boolean argument parsing
def str2bool(v):
  if v.lower() in ('yes', 'true', 't', 'y', '1'):
    return True
  elif v.lower() in ('no', 'false', 'f', 'n', '0'):
    return False
  else:
    raise argparse.ArgumentTypeError('Boolean value expected.')

###################################################################################################
# main
def main():
  global debug

  parser = argparse.ArgumentParser(description=scriptName, add_help=False, usage='{} <arguments>'.format(scriptName))
  parser.add_argument('-v', '--verbose', dest='debug', type=str2bool, nargs='?', const=True, default=False, help="Verbose output")
  parser.add_argument('-i', '--index', dest='index', metavar='<str>', type=str, default='arkime_sessions3-*', help='Index Pattern Name')
  parser.add_argument('-d', '--dashboards', dest='dashboardsUrl', metavar='<protocol://host:port>', type=str, default=os.getenv('DASHBOARDS_URL', 'http://dashboards:5601/dashboards'), help='Dashboards URL')
  parser.add_argument('-o', '--opensearch', dest='opensearchUrl', metavar='<protocol://host:port>', type=str, default=os.getenv('OPENSEARCH_URL', 'http://opensearch:9200'), help='OpenSearch URL')
  parser.add_argument('-t', '--template', dest='template', metavar='<str>', type=str, default=None, help='OpenSearch template to merge')
  parser.add_argument('-n', '--dry-run', dest='dryrun', type=str2bool, nargs='?', const=True, default=False, help="Dry run (no PUT)")
  try:
    parser.error = parser.exit
    args = parser.parse_args()
  except SystemExit:
    parser.print_help()
    exit(2)

  debug = args.debug
  if debug:
    eprint(os.path.join(scriptPath, scriptName))
    eprint("Arguments: {}".format(sys.argv[1:]))
    eprint("Arguments: {}".format(args))
  else:
    sys.tracebacklimit = 0

  # get version number so Dashboards doesn't think we're doing a XSRF when we do the PUT
  statusInfoResponse = requests.get('{}/{}'.format(args.dashboardsUrl, GET_STATUS_API))
  statusInfoResponse.raise_for_status()
  statusInfo = statusInfoResponse.json()
  dashboardsVersion = statusInfo['version']['number']
  if debug:
    eprint('OpenSearch Dashboards version is {}'.format(dashboardsVersion))

  opensearchInfoResponse = requests.get(args.opensearchUrl)
  opensearchInfo = statusInfoResponse.json()
  opensearchVersion = statusInfo['version']['number']
  if debug:
    eprint('OpenSearch version is {}'.format(opensearchVersion))

  # find the ID of the index name (probably will be the same as the name)
  getIndexInfoResponse = requests.get(
    '{}/{}'.format(args.dashboardsUrl, GET_INDEX_PATTERN_INFO_URI),
    params={
      'type': 'index-pattern',
      'fields': 'id',
      'search': '"{}"'.format(args.index)
    }
  )
  getIndexInfoResponse.raise_for_status()
  getIndexInfo = getIndexInfoResponse.json()
  indexId = getIndexInfo['saved_objects'][0]['id'] if (len(getIndexInfo['saved_objects']) > 0) else None
  if debug:
    eprint('Index ID for {} is {}'.format(args.index, indexId))

  if indexId is not None:

    # get the current fields list
    getFieldsResponse = requests.get('{}/{}'.format(args.dashboardsUrl, GET_FIELDS_URI),
                                     params={ 'pattern': args.index,
                                              'meta_fields': ["_source","_id","_type","_index","_score"] })
    getFieldsResponse.raise_for_status()
    getFieldsList = getFieldsResponse.json()['fields']
    fieldsNames = [field['name'] for field in getFieldsList if 'name' in field]

    # get the fields from the template, if specified, and merge those into the fields list
    if args.template is not None:
      try:

        # request template from OpenSearch and pull the mappings/properties (field list) out
        getTemplateResponse = requests.get('{}/{}/{}'.format(args.opensearchUrl, OS_GET_TEMPLATE_URI, args.template))
        getTemplateResponse.raise_for_status()
        getTemplateInfo = getTemplateResponse.json()[args.template]['mappings']['properties']

        # a field should be merged if it's not already in the list we have from Dashboards, and it's
        # in the list of types we're merging (leave more complex types like nested and geolocation
        # to be handled naturally as the data shows up)
        for field in getTemplateInfo:
          mergeFieldTypes = ("date", "float", "integer", "ip", "keyword", "long", "short", "text")
          if ((field not in fieldsNames) and
              ('type' in getTemplateInfo[field]) and
              (getTemplateInfo[field]['type'] in mergeFieldTypes)):

            # create field dict in same format as those returned by GET_FIELDS_URI above
            mergedFieldInfo = {}
            mergedFieldInfo['name'] = field
            mergedFieldInfo['esTypes'] = [ getTemplateInfo[field]['type'] ]
            if ((getTemplateInfo[field]['type'] == 'float') or
                (getTemplateInfo[field]['type'] == 'integer') or
                (getTemplateInfo[field]['type'] == 'long') or
                (getTemplateInfo[field]['type'] == 'short')):
              mergedFieldInfo['type'] = 'number'
            elif ((getTemplateInfo[field]['type'] == 'keyword') or
                  (getTemplateInfo[field]['type'] == 'text')):
              mergedFieldInfo['type'] = 'string'
            else:
              mergedFieldInfo['type'] = getTemplateInfo[field]['type']
            mergedFieldInfo['searchable'] = True
            mergedFieldInfo['aggregatable'] = ("text" not in mergedFieldInfo['esTypes'])
            mergedFieldInfo['readFromDocValues'] = mergedFieldInfo['aggregatable']
            fieldsNames.append(field)
            getFieldsList.append(mergedFieldInfo)

          # elif debug:
          #   eprint('Not merging {}: {}'.format(field, json.dumps(getTemplateInfo[field])))

      except Exception as e:
        eprint('"{}" raised for "{}", skipping template merge'.format(str(e), args.template))

    if debug:
      eprint('{} would have {} fields'.format(args.index, len(getFieldsList)))

    # define field formatting map for Dashboards -> Arkime drilldown and other URL drilldowns
    #
    # see: https://github.com/cisagov/Malcolm/issues/133
    #      https://github.com/mmguero-dev/kibana-plugin-drilldownmenu
    #
    # fieldFormatMap is
    #    {
    #        "source.ip": {
    #            "id": "drilldown",
    #            "params": {
    #                "parsedUrl": {
    #                    "origin": "https://malcolm.local.lan",
    #                    "pathname": "/kibana/app/kibana",
    #                    "basePath": "/kibana"
    #                },
    #                "urlTemplates": [
    #                    null,
    #                    {
    #                        "url": "/idkib2ark/source.ip == {{value}}",
    #                        "label": "Arkime: source.ip == {{value}}"
    #                    }
    #                ]
    #            }
    #        },
    #        ...
    #    }
    fieldFormatMap = {}
    for field in getFieldsList:
      if field['name'][:1].isalpha():

        # for Arkime to query by database field name, see arkime issue/PR 1461/1463
        valQuote = '"' if field['type'] == 'string' else ''
        valDbPrefix = '' if field['name'].startswith('zeek') else 'db:'
        drilldownInfoParamsUrlTemplateValues = {}
        drilldownInfoParamsUrlTemplateValues['url'] = '/idkib2ark/{}{} == {}{{{{value}}}}{}'.format(valDbPrefix, field['name'], valQuote, valQuote)
        drilldownInfoParamsUrlTemplateValues['label'] = 'Arkime {}: {}{{{{value}}}}{}'.format(field['name'], valQuote, valQuote)
        drilldownInfoParamsUrlTemplates = [None, drilldownInfoParamsUrlTemplateValues]

        if (field['type'] == 'ip') or (re.search(r'[_\.-](h|ip)$', field['name'], re.IGNORECASE) is not None):
          # add drilldown for searching IANA for IP addresses
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = 'https://www.virustotal.com/en/ip-address/{{value}}/information/'
          drilldownInfoParamsUrlTemplateValues['label'] = 'VirusTotal IP: {{value}}'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        elif re.search(r'(^|[\b_\.-])(md5|sha(1|256|384|512))\b', field['name'], re.IGNORECASE) is not None:
          # add drilldown for searching VirusTotal for hash signatures
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = 'https://www.virustotal.com/gui/file/{{value}}/detection'
          drilldownInfoParamsUrlTemplateValues['label'] = 'VirusTotal Hash: {{value}}'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        elif re.search(r'(^|[\b_\.-])(hit|signature(_?id))?s?$', field['name'], re.IGNORECASE) is not None:
          # add drilldown for searching the web for signature IDs
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = 'https://duckduckgo.com/?q="{{value}}"'
          drilldownInfoParamsUrlTemplateValues['label'] = 'Web Search: {{value}}'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        elif re.search(r'(^|src|dst|source|dest|destination|[\b_\.-])p(ort)?s?$', field['name'], re.IGNORECASE) is not None:
          # add drilldown for searching IANA for ports
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = 'https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.xhtml?search={{value}}'
          drilldownInfoParamsUrlTemplateValues['label'] = 'Port Registry: {{value}}'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        elif re.search(r'^(protocol?|network\.protocol)$', field['name'], re.IGNORECASE) is not None:
          # add drilldown for searching IANA for services
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = 'https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.xhtml?search={{value}}'
          drilldownInfoParamsUrlTemplateValues['label'] = 'Service Registry: {{value}}'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        elif re.search(r'^(network\.transport|ipProtocol)$', field['name'], re.IGNORECASE) is not None:
          # add URL link for assigned transport protocol numbers
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = 'https://www.iana.org/assignments/protocol-numbers/protocol-numbers.xhtml'
          drilldownInfoParamsUrlTemplateValues['label'] = 'Protocol Registry'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        elif re.search(r'(as\.number|(src|dst)ASN|asn\.(src|dst))$', field['name'], re.IGNORECASE) is not None:
          # add drilldown for searching ARIN for ASN
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = 'https://search.arin.net/rdap/?query={{value}}&searchFilter=asn'
          drilldownInfoParamsUrlTemplateValues['label'] = 'ARIN ASN: {{value}}'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        elif re.search(r'mime[_\.-]?type', field['name'], re.IGNORECASE) is not None:
          # add drilldown for searching mime/media/content types
          # TODO: '/' in URL is getting messed up somehow, maybe we need to url encode it manually? not sure...
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = 'https://www.iana.org/assignments/media-types/{{value}}'
          drilldownInfoParamsUrlTemplateValues['label'] = 'Media Type Registry: {{value}}'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        elif re.search(r'(^zeek\.files\.extracted$)', field['name'], re.IGNORECASE) is not None:
          # add download for extracted/quarantined zeek files
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = '/dl-extracted-files/quarantine/{{value}}'
          drilldownInfoParamsUrlTemplateValues['label'] = 'Download (if quarantined)'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)
          drilldownInfoParamsUrlTemplateValues = {}
          drilldownInfoParamsUrlTemplateValues['url'] = '/dl-extracted-files/preserved/{{value}}'
          drilldownInfoParamsUrlTemplateValues['label'] = 'Download (if preserved)'
          drilldownInfoParamsUrlTemplates.append(drilldownInfoParamsUrlTemplateValues)

        drilldownInfoParams = {}
        drilldownInfoParams['urlTemplates'] = drilldownInfoParamsUrlTemplates

        drilldownInfo = {}
        drilldownInfo['id'] = 'drilldown'
        drilldownInfo['params'] = drilldownInfoParams

        fieldFormatMap[field['name']] = drilldownInfo

    # set the index pattern with our complete list of fields
    putIndexInfo = {}
    putIndexInfo['attributes'] = {}
    putIndexInfo['attributes']['title'] = args.index
    putIndexInfo['attributes']['fields'] = json.dumps(getFieldsList)
    putIndexInfo['attributes']['fieldFormatMap'] = json.dumps(fieldFormatMap)

    if not args.dryrun:
      putResponse = requests.put('{}/{}/{}'.format(args.dashboardsUrl, PUT_INDEX_PATTERN_URI, indexId),
                                 headers={ 'Content-Type': 'application/json',
                                           'osd-xsrf': 'true',
                                           'osd-version': dashboardsVersion, },
                                 data=json.dumps(putIndexInfo))
      putResponse.raise_for_status()

    # if we got this far, it probably worked!
    if args.dryrun:
      print("success (dry run only, no write performed)")
    else:
      print("success")

  else:
    print("failure (could not find Index ID for {})".format(args.index))

if __name__ == '__main__':
  main()