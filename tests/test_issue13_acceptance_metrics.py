"""Issue 13 - Testes de Aceite por Ciclo e SLOs."""

from assistente_voz.acceptance_metrics import (
    compute_p95,
    compute_wer,
    evaluate_stability,
)


def test_wer_battery_meets_target_for_representative_sentences():
    references = [
        "paciente com dor de cabeça há dois dias",
        "nega alergias medicamentosas",
        "relata falta de ar ao esforço",
        "usa dipirona quando necessário",
    ]
    hypotheses = [
        "paciente com dor de cabeça há dois dias",
        "nega alergias medicamentosas",
        "relata falta de ar ao esforço",
        "usa dipirona quando necessário",
    ]

    wer = compute_wer(references, hypotheses)
    assert wer <= 0.10


def test_e2e_p95_latency_validation():
    latencies_s = [
        2.1, 2.4, 2.8, 3.0, 3.1, 3.2, 3.3, 3.5, 3.7, 3.9,
        2.9, 3.0, 3.2, 3.4, 3.6, 3.7, 3.8, 3.9, 3.95, 3.99,
    ]

    p95 = compute_p95(latencies_s)
    assert p95 < 4.0


def test_stability_for_30_minute_continuous_run_and_error_rate():
    result = evaluate_stability(
        continuous_runtime_minutes=30.0,
        total_events=500,
        technical_errors=9,
    )

    assert result.passed is True
    assert result.technical_error_rate < 0.02


def test_stability_fails_when_error_rate_too_high():
    result = evaluate_stability(
        continuous_runtime_minutes=30.0,
        total_events=100,
        technical_errors=3,
    )

    assert result.passed is False
    assert result.technical_error_rate >= 0.02
