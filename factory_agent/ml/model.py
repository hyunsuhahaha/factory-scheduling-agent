"""작업 소요 시간 예측 모델.

입력: 설비, 제품, 원료 계열, 수량, 계획 소요 시간, 교체 직후 여부, 야간 여부
출력: log(실제 시간 / 계획 시간) — 계획 대비 보정 배율을 예측한다.

학습은 PyTorch (GPU 워커에서 cuda 장치, 없으면 cpu). 예측은 numpy만으로 한다:
CPU 최적화 워커가 torch 없이 학습된 가중치를 쓸 수 있게, 모델을 JSON(가중치 리스트)으로 저장한다.
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime

import numpy as np

from .plant import is_night

HIDDEN = 32


def _vocab(records: list[dict], key: str) -> list[str]:
    return sorted({r[key] for r in records})


def featurize(rows: list[dict], vocab: dict[str, list[str]]) -> np.ndarray:
    """rows: machine_id, product_id, family, quantity, planned_minutes, after_setup(0~1), night(0~1)."""
    cols = []
    for key in ("machine_id", "product_id", "family"):
        index = {v: i for i, v in enumerate(vocab[key])}
        onehot = np.zeros((len(rows), len(index)), dtype=np.float32)
        for i, r in enumerate(rows):
            j = index.get(r[key])
            if j is not None:  # 학습 때 못 본 값은 전부 0 (평균적인 보정으로 예측)
                onehot[i, j] = 1.0
        cols.append(onehot)
    numeric = np.array(
        [[math.log(r["quantity"]), math.log(r["planned_minutes"]), r["after_setup"], r["night"]] for r in rows], dtype=np.float32
    )
    return np.concatenate(cols + [numeric], axis=1)


def _rows_from_records(records: list[dict]) -> tuple[list[dict], np.ndarray]:
    rows, targets = [], []
    for r in records:
        planned = r["quantity"] / r["planned_rate"] * 60
        started = r["started_at"] if isinstance(r["started_at"], datetime) else datetime.fromisoformat(r["started_at"])
        rows.append({
            "machine_id": r["machine_id"], "product_id": r["product_id"], "family": r["family"], "quantity": r["quantity"],
            "planned_minutes": planned, "after_setup": float(r["after_setup"]), "night": float(is_night(started)),
        })
        targets.append(math.log(r["actual_minutes"] / planned))
    return rows, np.array(targets, dtype=np.float32)


def train(records: list[dict], device: str = "cpu", epochs: int = 300, test_fraction: float = 0.2, seed: int = 0) -> dict:
    """실적으로 모델을 학습하고 (모델 JSON, 평가 지표)를 담은 dict를 돌려준다."""
    import torch

    if len(records) < 50:
        raise ValueError("need at least 50 production records to train")
    started = time.perf_counter()
    torch.manual_seed(seed)
    rng = random.Random(seed)
    records = list(records)
    rng.shuffle(records)
    n_test = max(1, int(len(records) * test_fraction))
    test, train_set = records[:n_test], records[n_test:]
    vocab = {k: _vocab(train_set, k) for k in ("machine_id", "product_id", "family")}

    rows, y = _rows_from_records(train_set)
    x = featurize(rows, vocab)
    mean, std = x.mean(axis=0), x.std(axis=0) + 1e-6
    xt = torch.tensor((x - mean) / std, device=device)
    yt = torch.tensor(y, device=device).unsqueeze(1)

    model = torch.nn.Sequential(
        torch.nn.Linear(x.shape[1], HIDDEN), torch.nn.ReLU(), torch.nn.Linear(HIDDEN, HIDDEN), torch.nn.ReLU(), torch.nn.Linear(HIDDEN, 1)
    ).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=3e-3, weight_decay=1e-4)
    for _ in range(epochs):  # 데이터가 작아 full-batch로 충분
        opt.zero_grad()
        loss = torch.nn.functional.mse_loss(model(xt), yt)
        loss.backward()
        opt.step()

    params = {
        "vocab": vocab,
        "mean": mean.tolist(),
        "std": std.tolist(),
        "layers": [[layer.weight.detach().cpu().numpy().tolist(), layer.bias.detach().cpu().numpy().tolist()]
                   for layer in model if isinstance(layer, torch.nn.Linear)],
    }
    metrics = evaluate(params, test)
    metrics |= {"train_records": len(train_set), "test_records": len(test), "device": device,
                "epochs": epochs, "train_seconds": round(time.perf_counter() - started, 2)}
    return {"params": params, "metrics": metrics}


def predict_log_ratio(params: dict, rows: list[dict]) -> np.ndarray:
    x = (featurize(rows, params["vocab"]) - np.array(params["mean"], dtype=np.float32)) / np.array(params["std"], dtype=np.float32)
    layers = params["layers"]
    for i, (w, b) in enumerate(layers):
        x = x @ np.array(w, dtype=np.float32).T + np.array(b, dtype=np.float32)
        if i < len(layers) - 1:
            x = np.maximum(x, 0)
    return x[:, 0]


def predict_minutes(params: dict, rows: list[dict]) -> np.ndarray:
    planned = np.array([r["planned_minutes"] for r in rows], dtype=np.float32)
    return planned * np.exp(predict_log_ratio(params, rows))


def evaluate(params: dict, records: list[dict]) -> dict:
    """테스트 실적에서 '계획 속도 그대로' 대비 예측 오차."""
    rows, _ = _rows_from_records(records)
    actual = np.array([r["actual_minutes"] for r in records])
    planned = np.array([r["planned_minutes"] for r in rows])
    predicted = predict_minutes(params, rows)

    def stats(est):
        err = np.abs(est - actual)
        return round(float(err.mean()), 1), round(float((err / actual).mean() * 100), 1)

    mae_p, mape_p = stats(planned)
    mae_m, mape_m = stats(predicted)
    return {"mae_minutes_planned": mae_p, "mape_planned_pct": mape_p, "mae_minutes_model": mae_m, "mape_model_pct": mape_m}


def train_payload(records: list[dict], device: str, epochs: int, test_fraction: float) -> dict:
    """프로세스 풀 진입점."""
    return train(records, device=device, epochs=epochs, test_fraction=test_fraction)
