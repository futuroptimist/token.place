from pathlib import Path

import yaml


def test_raspi_pod_manifest_valid():
    manifest = Path(__file__).resolve().parents[2] / 'k8s' / 'relay-raspi-pod.yaml'
    with manifest.open() as f:
        data = yaml.safe_load(f)

    assert data['kind'] == 'Pod'
    assert data['metadata']['name'] == 'tokenplace-relay-raspi'

    spec = data['spec']
    assert spec['nodeSelector']['kubernetes.io/arch'] == 'arm64'

    container = spec['containers'][0]
    assert 'relay.py' in container['command'][-1]
    assert container['ports'][0]['containerPort'] == 5010
