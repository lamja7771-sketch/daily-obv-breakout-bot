def get_contracts():
    url = f"{GATE_URL}/futures/usdt/contracts"

    response = session.get(
        url,
        timeout=20
    )

    response.raise_for_status()

    contracts = response.json()

    result = []

    for contract in contracts:
        name = contract.get("name", "")
        status = contract.get("status", "")

        # Only USDT futures contracts
        if not name.endswith("_USDT"):
            continue

        # Only contracts currently trading
        if status != "trading":
            continue

        result.append(name)

    return sorted(set(result))
