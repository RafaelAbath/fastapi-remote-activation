"""Script de RPA simulado, para desenvolvimento local da API.

Mostra o protocolo esperado dos scripts reais:
- recebe `--limit N` (opcional) e as variáveis RPA_JOB_ID / RPA_EXECUTION_ID;
- cada linha impressa vira log (`log_tail`);
- linhas `::extracao {...}` / `::designacao {...}` são repassadas ao painel (banco de senhas)
  e contam automaticamente `extraidos` / `designados`;
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

SETORES = ["AUTORIZAÇÃO", "MEDICAMENTO", "OPME", "FACEC.OPME", "HOME CARE", "ODONTO"]

print("login ok", flush=True)
pages = max(1, args.limit // 10)
for page in range(1, pages + 1):
    time.sleep(0.5)
    print(f"página {page}/{pages}", flush=True)
    senhas = [str(90000000 + n) for n in range((page - 1) * 10, min(page * 10, args.limit))]
    if args.mode == "extracao":
        print("::extracao " + json.dumps([{"senha": s} for s in senhas]), flush=True)
    else:
        itens = [{"senha": s, "setor": SETORES[int(s) % len(SETORES)], "status": "Em análise"} for s in senhas]
        print("::designacao " + json.dumps(itens, ensure_ascii=False), flush=True)
print("concluído", flush=True)
sys.exit(0)
