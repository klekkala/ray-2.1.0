import contextlib
import pytest
from ray.air import session
from ray.air.checkpoint import Checkpoint
from ray.train.torch.torch_checkpoint import TorchCheckpoint
import torch

import ray
from ray.air.examples.pytorch.torch_linear_example import (
    train_func as linear_train_func,
)
from ray.train.torch import TorchPredictor, TorchTrainer
from ray.tune import TuneError
from ray.air.config import ScalingConfig
from ray.train.torch import TorchConfig
import ray.train as train
from unittest.mock import patch
from ray.cluster_utils import Cluster


@pytest.fixture
def ray_start_4_cpus():
    address_info = ray.init(num_cpus=4)
    yield address_info
    # The code after the yield will run as teardown code.
    ray.shutdown()


@contextlib.contextmanager
def ray_start_2_node_cluster(num_cpus_per_node: int, num_gpus_per_node: int):
    cluster = Cluster()
    for _ in range(2):
        cluster.add_node(num_cpus=num_cpus_per_node, num_gpus=num_gpus_per_node)

    ray.init(address=cluster.address)

    yield

    ray.shutdown()
    cluster.shutdown()


@pytest.mark.parametrize("num_workers", [1, 2])
def test_torch_linear(ray_start_4_cpus, num_workers):
    def train_func(config):
        result = linear_train_func(config)
        assert len(result) == epochs
        assert result[-1]["loss"] < result[0]["loss"]

    num_workers = num_workers
    epochs = 3
    scaling_config = ScalingConfig(num_workers=num_workers)
    config = {"lr": 1e-2, "hidden_size": 1, "batch_size": 4, "epochs": epochs}
    trainer = TorchTrainer(
        train_loop_per_worker=train_func,
        train_loop_config=config,
        scaling_config=scaling_config,
    )
    trainer.fit()


def test_torch_e2e(ray_start_4_cpus):
    def train_func():
        model = torch.nn.Linear(1, 1)
        session.report({}, checkpoint=Checkpoint.from_dict(dict(model=model)))

    scaling_config = ScalingConfig(num_workers=2)
    trainer = TorchTrainer(
        train_loop_per_worker=train_func, scaling_config=scaling_config
    )
    result = trainer.fit()

    predict_dataset = ray.data.range(3)

    class TorchScorer:
        def __init__(self):
            self.pred = TorchPredictor.from_checkpoint(result.checkpoint)

        def __call__(self, x):
            return self.pred.predict(x, dtype=torch.float)

    predictions = predict_dataset.map_batches(
        TorchScorer, batch_format="pandas", compute="actors"
    )
    assert predictions.count() == 3


def test_torch_e2e_state_dict(ray_start_4_cpus):
    def train_func():
        model = torch.nn.Linear(1, 1).state_dict()
        session.report({}, checkpoint=Checkpoint.from_dict(dict(model=model)))

    scaling_config = ScalingConfig(num_workers=2)
    trainer = TorchTrainer(
        train_loop_per_worker=train_func, scaling_config=scaling_config
    )
    result = trainer.fit()

    # If loading from a state dict, a model definition must be passed in.
    with pytest.raises(ValueError):
        TorchPredictor.from_checkpoint(result.checkpoint)

    class TorchScorer:
        def __init__(self):
            self.pred = TorchPredictor.from_checkpoint(
                result.checkpoint, model=torch.nn.Linear(1, 1)
            )

        def __call__(self, x):
            return self.pred.predict(x, dtype=torch.float)

    predict_dataset = ray.data.range(3)
    predictions = predict_dataset.map_batches(
        TorchScorer, batch_format="pandas", compute="actors"
    )
    assert predictions.count() == 3


def test_checkpoint_freq(ray_start_4_cpus):
    # checkpoint_freq is not supported so raise an error
    trainer = TorchTrainer(
        train_loop_per_worker=lambda config: None,
        scaling_config=ray.air.ScalingConfig(num_workers=1),
        run_config=ray.air.RunConfig(
            checkpoint_config=ray.air.CheckpointConfig(
                checkpoint_frequency=2,
            ),
        ),
    )
    with pytest.raises(TuneError):
        trainer.fit()


@pytest.mark.parametrize(
    "num_gpus_per_worker,expected_devices", [(0.5, [0]), (1, [0]), (2, [0, 1])]
)
def test_tune_torch_get_device_gpu(num_gpus_per_worker, expected_devices):
    """Tests if GPU ids are set correctly when running train concurrently in nested actors
    (for example when used with Tune).
    """
    from ray.air.config import ScalingConfig
    import time

    num_samples = 2
    num_workers = 2

    # We should have exactly enough resources in the cluster to run both samples
    # concurrently.
    total_gpus_required = num_workers * num_gpus_per_worker * num_samples
    # Divide by two because of a 2 node cluster.
    gpus_per_node = total_gpus_required // 2

    # Use the same number of cpus per node as gpus per node.
    with ray_start_2_node_cluster(
        num_cpus_per_node=gpus_per_node, num_gpus_per_node=gpus_per_node
    ):

        @patch("torch.cuda.is_available", lambda: True)
        def train_fn():
            # We use STRICT_SPREAD strategy to force multiple samples on the same node.
            # For single or fractional GPU case, each worker has only 1 visible device (
            # the other is taken by the other sample) so device index should be 0.
            # For the multiple GPU case, each worker has 2 visible devices so device
            # index should be either 0 or 1. It doesn't matter which.
            assert train.torch.get_device().index in expected_devices

        @ray.remote(num_cpus=0)
        class TrialActor:
            def __init__(self, warmup_steps):
                # adding warmup_steps to the config
                # to avoid the error of checkpoint name conflict
                time.sleep(2 * warmup_steps)
                self.trainer = TorchTrainer(
                    train_fn,
                    torch_config=TorchConfig(backend="gloo"),
                    scaling_config=ScalingConfig(
                        num_workers=num_workers,
                        use_gpu=True,
                        resources_per_worker={"CPU": 1, "GPU": num_gpus_per_worker},
                        # Need to specify 0 trainer resources so STRICT_SPREAD
                        # will work.
                        trainer_resources={"CPU": 0},
                        placement_strategy="STRICT_SPREAD",
                        # Each gpu worker will be spread onto separate nodes. This
                        # forces different samples to run concurrently on the same
                        # node.
                    ),
                )

            def run(self):
                return self.trainer.fit()

        actors = [TrialActor.remote(1) for _ in range(num_samples)]
        ray.get([actor.run.remote() for actor in actors])


def test_torch_auto_unwrap(ray_start_4_cpus):
    """Tests if underlying model from DDP is extracted when saving ckpt."""

    def train_fn():
        model = torch.nn.Linear(1, 1)

        # Wrap in DDP.
        model = train.torch.prepare_model(model)

        # Save DDP wrapped model.
        session.report({"model": model}, checkpoint=TorchCheckpoint.from_model(model))

    trainer = TorchTrainer(
        train_loop_per_worker=train_fn,
        scaling_config=ScalingConfig(num_workers=2),
    )
    results = trainer.fit()

    last_checkpoint = results.checkpoint
    model = last_checkpoint.get_model()
    assert isinstance(model, torch.nn.Module) and not isinstance(
        model, torch.nn.parallel.DistributedDataParallel
    )

    model_report = results.metrics["model"]
    assert isinstance(model_report, torch.nn.Module) and not isinstance(
        model_report, torch.nn.parallel.DistributedDataParallel
    )


def test_torch_amp(ray_start_4_cpus):
    def train_fn():
        train.torch.accelerate(amp=True)
        model = torch.nn.Linear(1, 1)
        model = train.torch.prepare_model(model)

        session.report({}, checkpoint=TorchCheckpoint.from_model(model))

    trainer = TorchTrainer(
        train_fn,
        scaling_config=ScalingConfig(num_workers=2),
    )
    results = trainer.fit()
    assert results.checkpoint


def test_torch_amp_with_custom_get_state(ray_start_4_cpus):
    """Tests amp with a model that has a custom __getstate__ method defined.

    See https://discuss.ray.io/t/ray-train-hangs-for-long-time/6333/7
    """

    def train_fn():
        train.torch.accelerate(amp=True)

        class CustomLinear(torch.nn.Linear):
            def __getstate__(self):
                return self.__dict__.copy()

        model = CustomLinear(1, 1)
        model = train.torch.prepare_model(model)

        # Make sure model is serializable even with amp enabled.
        session.report({}, checkpoint=TorchCheckpoint.from_model(model))

    trainer = TorchTrainer(
        train_fn,
        scaling_config=ScalingConfig(num_workers=2),
    )
    results = trainer.fit()
    assert results.checkpoint


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-x", __file__]))