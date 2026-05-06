from assistente_voz.bootstrap import bootstrap_app


def main() -> int:
    result = bootstrap_app()
    print(f"OK profile_id={result.profile_id} llm_provider={result.llm_provider}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
