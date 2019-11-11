import os
import pyaml
import multiprocessing

from pathlib import Path
from datetime import datetime
from operator import itemgetter

import numpy as np

import torch
import torch.utils.data

from ignite.engine import Engine, Events
from ignite.contrib.handlers import ProgressBar

from . import features
from .config import parse_args
from .utils import git_info, cuda_info, sort_dict, round_timedelta, load_model
from .data import DecoyBatch
from .dataset import ProteinQualityTarget
from .metrics import customize_state, LocalMetrics, GlobalMetrics
from .ignite_commons import setup_testing, update_metrics, save_figures

# region Arguments parsing
ex = parse_args(config={
    # Experiment defaults
    'model': {},
    'data': {},

    # Test session defaults
    'test': {
        'data': {
            'input': os.environ.get('DATA_FOLDER', './data'),
            'output': os.environ.get('OUTPUT_FOLDER', './test'),
            'in_memory': True,
        },
        'batch_size': 1,
        'cpus': multiprocessing.cpu_count() - 1,
        'device': 'cuda' if torch.cuda.is_available() else 'cpu',
    }
})
test_session: dict = ex['test']


# Experiment: checks and computed fields
ex['data'].setdefault('residues', True)
ex['data'].setdefault('partial_entropy', True)
ex['data'].setdefault('self_information', True)
ex['data'].setdefault('dssp', True)

if ex['model']['fn'] is None:
    raise ValueError('Model constructor function not defined')

# Session computed fields
test_session['samples'] = 0
test_session['status'] = 'NEW'
test_session['datetime_started'] = None
test_session['datetime_completed'] = None
test_session['git'] = git_info()
test_session['cuda'] = cuda_info() if 'cuda' in test_session['device'] else None
test_session['metric'] = {}

if test_session['cpus'] < 0:
    raise ValueError(f'Invalid number of cpus: {test_session["cpus"]}')

# Print config so far
sort_dict(ex, [
    'name', 'tags', 'fullname', 'comment', 'completed_epochs',
    'samples', 'data', 'model', 'optimizer', 'loss', 'history'
])
sort_dict(test_session, ['data', 'batch_size', 'cpus', 'device', 'samples', 'status', 'datetime_started',
                         'datetime_completed', 'metric', 'git', 'cuda'])

pyaml.pprint(ex, safe=True, sort_dicts=False, force_embed=True, width=200)
# endregion

# region Building phase


# Saver
output_path = Path(test_session['data']['output']).expanduser().resolve()
output_path.mkdir(parents=True, exist_ok=True)
with open(output_path / 'test.yaml', 'w') as f:
    pyaml.dump(ex, f, safe=True, sort_dicts=False, force_embed=True)


# Model and optimizer
model = load_model(ex['model']).to(test_session['device'])
model.load_state_dict(torch.load(Path(ex['model']['state_dict']).expanduser(), map_location=test_session['device']))


# Dataset and dataloader
def get_dataloader(ex, test_session):
    node_features = [k for k in ('partial_entropy', 'self_information', 'dssp') if ex['data'][k]]
    cutoff = ex['data']['cutoff']

    targets = []
    folder = Path(test_session['data']['input']).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f'Not a directory: {folder}')
    # with open(folder / 'dataset_stats.yaml') as f:
    #     max_sequence_length = yaml.safe_load(f)['max_length']
    for target in sorted(folder.glob('*.npz')):
        targets.append(ProteinQualityTarget(target, node_features=node_features, cutoff=cutoff))

    if test_session['data']['in_memory']:
        for target in targets:
            target.load_eager()

    dataset_test = torch.utils.data.ConcatDataset(targets)

    dataloader_kwargs = dict(
        num_workers=test_session['cpus'],
        pin_memory='cuda' in test_session['device'],
        worker_init_fn=lambda _: np.random.seed(int(torch.initial_seed()) % (2 ** 32 - 1)),
        batch_size=test_session['batch_size'],
        collate_fn=DecoyBatch.collate,
    )
    return torch.utils.data.DataLoader(dataset_test, shuffle=False, **dataloader_kwargs)


dataloader_test = get_dataloader(ex, test_session)
# endregion


def test_function(tester, batch):
    batch = batch.to(test_session['device'])
    results = model(batch)

    return {
        'num_samples': len(batch),
        'results': results,
    }


def session_start(tester, session):
    session['status'] = 'RUNNING'
    session['datetime_started'] = datetime.utcnow()

    with open(output_path / 'test.yaml', 'w') as f:
        pyaml.dump(ex, f, safe=True, sort_dicts=False, force_embed=True)


def update_samples(engine: Engine, ex, session):
    session['samples'] += engine.state.output['num_samples']


def handle_failure(engine, e, name, ex, test_session):
    print(f'Exception raised during {name}, samples {test_session["samples"]}')
    print(e)

    # Log session failure to yaml
    test_session['status'] = 'FAILED'
    with open(output_path / 'test.yaml', 'w') as f:
        pyaml.dump(ex, f, safe=True, sort_dicts=False, force_embed=True)

    raise e


def session_end(tester, test_session):
    test_session['status'] = 'COMPLETED'
    test_session['datetime_completed'] = datetime.utcnow()
    elapsed = test_session["datetime_completed"] - test_session["datetime_started"]

    print(f'Tested {test_session["samples"]} protein models in {round_timedelta(elapsed)}')

    if 'cuda' in test_session['device']:
        for device_id, device_info in test_session['cuda']['devices'].items():
            device_info.update({
                'memory_used_max': f'{torch.cuda.max_memory_allocated(device_id) // (10**6)} MiB',
                'memory_cached_max': f'{torch.cuda.max_memory_cached(device_id) // (10**6)} MiB',
            })
        print(pyaml.dump(test_session['cuda']['devices'], safe=True, sort_dicts=False), sep='\n')

    print(pyaml.dump(test_session['metric']))

    # Need to save again because we updated test_session and gpu info
    with open(output_path / 'test.yaml', 'w') as f:
        pyaml.dump(ex, f, safe=True, sort_dicts=False, force_embed=True)


tester = Engine(test_function)
ot = itemgetter('results')
local_lddt_metrics = LocalMetrics(features.Output.Node.LOCAL_LDDT, title='LDDT', output_transform=ot)
local_lddt_metrics.attach(tester, 'local_lddt')

global_gdtts_metrics = GlobalMetrics(features.Output.Global.GLOBAL_GDTTS, title='GDT-TS', output_transform=ot,
                                     figures=('hist', 'paged_funnels', 'recall_at_k', 'ndcg_at_k'))
global_gdtts_metrics.attach(tester, 'global_gdtts')

# During testing, the progress bar shows the number of batches processed so far
pbar_test = ProgressBar(desc='Test')
pbar_test.attach(tester)

tester.add_event_handler(Events.STARTED, session_start, test_session)
tester.add_event_handler(Events.STARTED, customize_state)
tester.add_event_handler(Events.STARTED, setup_testing, model)
tester.add_event_handler(Events.EXCEPTION_RAISED, handle_failure, 'testing', ex, test_session)

tester.add_event_handler(Events.ITERATION_COMPLETED, update_samples, ex, test_session)

tester.add_event_handler(Events.COMPLETED, update_metrics, test_session)
tester.add_event_handler(Events.COMPLETED, save_figures, output_path)
tester.add_event_handler(Events.COMPLETED, session_end, test_session)

tester.run(dataloader_test)
