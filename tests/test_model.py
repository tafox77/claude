import numpy as np

from cryptobot.config import ModelConfig
from cryptobot.model import OnlineModel


def stream(n, rng, signal):
    X = rng.normal(size=(n, 5))
    p = 1 / (1 + np.exp(-signal * X[:, 0]))
    y = (rng.random(n) < p).astype(float)
    r = np.where(y == 1, 0.01, -0.008)
    return X, y, r


def train(signal, n=6000, seed=0):
    rng = np.random.default_rng(seed)
    model = OnlineModel(ModelConfig(min_train_samples=500))
    for _ in range(n // 20):
        model.learn(*stream(20, rng, signal))
    return model


def test_learns_a_real_pattern_and_becomes_trusted():
    model = train(signal=1.5)
    assert model.is_trustworthy(), model.stats()
    assert model.predict_proba(np.array([[2, 0, 0, 0, 0]]))[0] > 0.8


def test_refuses_to_trade_pure_noise():
    for seed in range(3):
        model = train(signal=0.0, seed=seed)
        assert not model.is_trustworthy(), model.stats()


def test_untrained_model_is_neutral_and_untrusted():
    model = OnlineModel(ModelConfig())
    assert model.predict_proba(np.zeros((1, 5)))[0] == 0.5
    assert not model.is_trustworthy()


def test_adapts_when_the_pattern_flips():
    rng = np.random.default_rng(1)
    model = OnlineModel(ModelConfig())
    for _ in range(300):
        model.learn(*stream(20, rng, 2.0))
    assert model.predict_proba(np.array([[2, 0, 0, 0, 0]]))[0] > 0.7
    for _ in range(300):
        model.learn(*stream(20, rng, -2.0))
    assert model.predict_proba(np.array([[2, 0, 0, 0, 0]]))[0] < 0.3


def test_save_and_load_round_trip(tmp_path):
    model = train(signal=1.5, n=2000)
    path = tmp_path / "m.joblib"
    model.save(path)
    loaded = OnlineModel.load_or_new(path, ModelConfig())
    x = np.ones((3, 5))
    np.testing.assert_allclose(loaded.predict_proba(x), model.predict_proba(x))
    assert loaded.n_seen == model.n_seen
