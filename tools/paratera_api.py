"""Read-only Paratera API access using the official, checksum-pinned SDK.

Credentials come from environment variables or an ignored 0600 JSON file.
Only documented read-only actions are accepted; no instance creation or shutdown.
"""

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import types


SDK_SHA256 = 'f8070db632ce0729f802d86e96962976fb05437389bee04442cfb3d8dc18f168'
ROOT = Path(__file__).resolve().parents[1]
CLIENTS = {
    'region': ('region.RegionClientMain', 'RegionClient', {'DescribeZones'}),
    'product': ('product.ProductClientMain', 'ProductClient', {
        'DescribeInstanceTypes', 'DescribeImages', 'DescribeDiskTypes', 'DescribeNetworkTypes'}),
    'ack_product': ('ack_product.ACKProductClientMain', 'ACKProductClient', {
        'DescribeACKServiceTypes', 'DescribeACKVolumeTypes',
        'DescribeACKPublicImages', 'DescribeACKAvailableResources'}),
    'ackcs': ('ackcs.ACKCSClientMain', 'ACKCSClient', {
        'DescribeServices', 'InquiryPriceCreateServices'}),
    'ackci': ('ackci.ACKCIClientMain', 'ACKCIClient', {'DescribeImages'}),
    'ecs': ('ecs.ECSClientMain', 'ECSClient', {
        'DescribeInstances', 'InquiryPriceRunInstances'}),
    'ack_job': ('ack_jobs.ACKJobClientMain', 'ACKJobClient', {'DescribeJobs'}),
}
# No full instance responses are printed or saved. Additional fields must be
# reviewed before inclusion, since DescribeServices/DescribeInstances can carry
# generated passwords and connection tokens.
SAFE_FIELDS = {
    'code', 'data', 'rows', 'total', 'pageNum', 'pageSize',
    'regionCode', 'regionName', 'zoneCode', 'zoneName', 'zone', 'zones',
    'serviceUuid', 'ecsUuid', 'aliasName', 'serviceModel', 'instanceType',
    'serviceStatus', 'resourceStatus', 'status', 'billingType',
    'imageUuid', 'imageName', 'imageType', 'imageOs', 'imageVersion',
    'imageArch', 'imageDesc', 'imageStatus', 'vhostType', 'platformName',
    'platformVersion', 'createdTime', 'updatedTime', 'gpu', 'gpuType',
    'gpuCount', 'gpuMemory', 'cpu', 'cpuCount', 'memory', 'memorySize',
    'diskSize', 'rootDiskSize', 'rootDiskType', 'serviceTypes', 'instanceTypes',
    'resourceUuid', 'serviceName', 'serviceType', 'vcpus', 'gpuSize',
    'available', 'soldOut', 'volumeSize', 'volumeType', 'minSize', 'maxSize',
    'serviceCpus', 'serviceMemory', 'serviceGpus', 'serviceGpumem',
    'localSysdiskType', 'localSysdiskSize', 'instanceCpus', 'instanceMemory',
    'instanceGpus', 'instanceGpumem', 'instanceModel', 'instanceSeries',
    'cpuPlatform', 'availableResources', 'availableCount', 'count',
    'imageOsType', 'imageOsVersion', 'imageSize', 'minDiskSize',
    'originPrice', 'discountPrice', 'tradePrice', 'price', 'prices', 'details',
    'resourceType', 'resourceName', 'unitPrice', 'unit', 'currency',
    'amount', 'discount', 'chargeType', 'billingUnit', 'billUnit',
    'vhostModel', 'vhostCpus', 'vhostMemory', 'vhostGpus', 'vhostGpumem',
    'rootDiskTypes', 'items', 'subResourceType', 'attr', 'size',
    'imageId', 'diskType', 'diskName', 'minDiskSize', 'maxDiskSize',
    'networkType', 'networkName', 'networkSize', 'minNetworkSize', 'maxNetworkSize',
    'jobUuid', 'done', 'success', 'progress', 'type', 'stoppedMode',
    'image', 'imageActive', 'createdAt', 'updatedAt',
}


def filter_response(value):
    if isinstance(value, list):
        return [filter_response(item) for item in value]
    if isinstance(value, dict):
        return {key: filter_response(item) for key, item in value.items() if key in SAFE_FIELDS}
    return value


def sdk_client(service):
    wheel = ROOT / '.cache/paratera/aicloud_sdk_python-3.0-py3-none-any.whl'
    with wheel.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != SDK_SHA256:
            raise ValueError('Official SDK wheel checksum mismatch')
    # The official SDK imports CPython's private _sha256 module, unavailable on
    # this Python 3.12 build. Keep the wheel intact and use the public hash.
    try:
        importlib.import_module('_sha256')
    except ModuleNotFoundError:
        compatibility = types.ModuleType('_sha256')
        compatibility.sha256 = hashlib.sha256
        sys.modules['_sha256'] = compatibility
    module, name, _ = CLIENTS[service]
    return getattr(importlib.import_module(module), name)


def credentials(path):
    access = os.environ.get('PARATERA_ACCESS_KEY')
    secret = os.environ.get('PARATERA_SECRET_ACCESS_KEY')
    if bool(access) != bool(secret):
        raise ValueError('Set both credential environment variables together')
    if not (access and secret) and path.exists():
        if path.is_symlink() or not path.is_file():
            raise ValueError('Credential file must be a regular private file')
        if path.stat().st_mode & 0o077:
            raise ValueError('Credential file must have mode 0600')
        content = json.loads(path.read_text())
        access, secret = content.get('access_key'), content.get('secret_access_key')
    if not (access and secret):
        raise ValueError('AccessKey and SecretAccessKey are both required; a single API key is insufficient')
    return access, secret


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('service', choices=CLIENTS)
    parser.add_argument('action')
    parser.add_argument('--payload-file', type=Path)
    parser.add_argument('--credentials', type=Path, default=ROOT / '.paratera/credentials.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check-sdk', action='store_true')
    parser.add_argument('--summary', action='store_true', help='print only counts; requires --output')
    args = parser.parse_args()
    if args.summary and not args.output:
        parser.error('--summary requires --output')
    if args.action not in CLIENTS[args.service][2]:
        parser.error('Only the documented read-only actions in this tool are allowed')
    try:
        cls = sdk_client(args.service)
        if args.check_sdk:
            print(json.dumps({'sdk_import': 'pass', 'sdk_sha256': SDK_SHA256,
                              'client': cls.__name__, 'authenticated_request_sent': False}))
            return
        access, secret = credentials(args.credentials)
        from common.ClientProfileMain import ClientProfile
        from common.HttpProfileMain import HttpProfile
        client = cls(HttpProfile(verifyCert=True, connectTimeout=10, readTimeout=30), ClientProfile(access, secret))
        payload = json.loads(args.payload_file.read_text()) if args.payload_file else {}
        response = client.GenericRequest(payload, args.action, lambda data: data)
        safe = filter_response(response)
        # Error strings can echo request fields. Retain codes, withhold arbitrary
        # server text and never include credentials or signed request headers.
        safe.pop('message', None)
        if response.get('code') != 200:
            print(json.dumps({'code': response.get('code'), 'request_failed': True}))
            raise SystemExit(1)
        rendered = json.dumps(safe, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
        if args.summary:
            data = safe.get('data')
            print(json.dumps({'code': safe.get('code'), 'output': str(args.output),
                              'data_count': len(data) if isinstance(data, list) else None}))
        else:
            print(rendered, end='')
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    except Exception as error:
        # SDK/network exceptions may contain request URLs or echoed credentials.
        parser.exit(1, f'API query failed ({type(error).__name__}); details withheld.\n')


if __name__ == '__main__':
    main()
