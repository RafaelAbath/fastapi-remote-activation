"""Script de RPA simulado, para desenvolvimento local da API.

Mostra o protocolo esperado dos scripts reais:
- recebe `--limit N` (opcional) e as variáveis RPA_JOB_ID / RPA_EXECUTION_ID;
- cada linha impressa vira log (`log_tail`);
- linhas `::metrics {...}` atualizam as métricas do job;
- código de saída 0 = SUCCEEDED, qualquer outro = FAILED.
"""

import argparse
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["extracao", "designacao"])
parser.add_argument("--limit", type=int, default=50)
args = parser.parse_args()

metric = "extraidos" if args.mode == "extracao" else "designados"
print("login ok", flush=True)
pages = max(1, args.limit // 10)
for page in range(1, pages + 1):
    time.sleep(0.5)
    print(f"página {page}/{pages}", flush=True)
    print("::metrics " + json.dumps({metric: min(page * 10, args.limit)}), flush=True)
print("concluído", flush=True)
sys.exit(0)
