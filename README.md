# fastapi-remote-activation

API HTTP instalada em cada VM Ubuntu de RPA. Recebe disparos do backend Laravel do SIAGS Flow,
executa **apenas** scripts da allowlist local como processos e informa o status.
Contrato: [docs/contrato-api-vm.md](docs/contrato-api-vm.md).

## Rodando localmente

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env            # ajuste VM_API_TOKEN e VM_SCRIPTS
.venv/bin/uvicorn app.main:create_app --factory --port 8000
```

```bash
TOKEN=troque-este-token
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/health
curl -X POST localhost:8000/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "Idempotency-Key: exec-1" \
  -d '{"script": "extracao", "execution_id": "exec-1", "parameters": {"limit": 30}}'
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/jobs/<job_id>
```

Testes: `.venv/bin/pytest`.

## Endpoints

| Método | Rota | Resposta |
|---|---|---|
| GET | `/api/v1/health` | `200` `{status, version, running_jobs}` |
| POST | `/api/v1/jobs` | `202` `{job_id, execution_id, status}` · `422` script/parâmetro inválido · `409` Idempotency-Key reutilizado com outro payload |
| GET | `/api/v1/jobs/{job_id}` | `200` status, métricas, `log_tail`, erro · `404` |
| POST | `/api/v1/jobs/{job_id}/cancel` | `202` · `409` job já finalizado · `404` |

Todas exigem `Authorization: Bearer <VM_API_TOKEN>` (senão `401`). Erros sempre trazem `detail` em texto.
A documentação interativa (`/docs`, `/openapi.json`) fica desligada.

## Configuração (`.env` ou variáveis de ambiente)

| Variável | Padrão | |
|---|---|---|
| `VM_API_TOKEN` | — (obrigatória) | Token da VM cadastrado no painel |
| `VM_SCRIPTS` | `{}` | JSON `nome -> argv`. Só esses nomes são aceitos |
| `VM_SCRIPTS_CWD` | diretório atual | Diretório de trabalho dos scripts |
| `VM_DATA_DIR` | `data` | Onde fica o SQLite dos jobs |
| `VM_MAX_CONCURRENT_JOBS` | `1` | Jobs rodando ao mesmo tempo; os demais ficam `QUEUED` |
| `VM_JOB_TIMEOUT_SECONDS` | sem limite | Após isso o job é encerrado como `FAILED` |
| `VM_CANCEL_GRACE_SECONDS` | `10` | Tempo entre SIGTERM e SIGKILL no cancelamento |
| `VM_LOG_TAIL_LINES` | `200` | Linhas mantidas em `log_tail` |
| `VM_TIMEZONE` | `America/Sao_Paulo` | Fuso dos timestamps |
| `VM_PAINEL_URL` | sem envio | URL do painel (ex.: `https://painel.exemplo.com.br`) para onde vão os resultados |
| `VM_INGESTAO_INTERVALO_SECONDS` | `5` | Intervalo entre envios ao painel |
| `VM_INGESTAO_RETENCAO_DIAS` | `7` | Por quanto tempo itens já enviados ficam no SQLite |

## Protocolo dos scripts

Veja [examples/fake_rpa.py](examples/fake_rpa.py).

- O comando vem da allowlist; nada da requisição é interpretado como comando.
  `limit` é repassado como `--limit N` e em `RPA_LIMIT`. Também recebem `RPA_JOB_ID` e `RPA_EXECUTION_ID`.
- Variáveis `VM_*` (incluindo o token) **não** são repassadas aos scripts.
- stdout/stderr viram `log_tail` (com horário). Padrões como `senha=...`, `token: ...` e `Bearer ...`
  são mascarados, mas o script não deve imprimir dados sensíveis.
- Linha `::metrics {"extraidos": 120}` atualiza as métricas (valores numéricos, mesclados).
- Linhas `::extracao` e `::designacao` são resultados para o banco de senhas do painel (ver abaixo).
- Saída `0` → `SUCCEEDED`; outro código → `FAILED` com a última linha de log no `error`.
- Cancelamento envia SIGTERM ao grupo de processos e SIGKILL após o tempo de tolerância.

## Envio dos resultados ao painel

O script imprime cada resultado numa linha; a API guarda, agrupa e envia ao painel
(`POST {VM_PAINEL_URL}/api/rpa/ingestao/extracoes` e `.../designacoes`) com o `execution_id` do job e
o mesmo `VM_API_TOKEN` (o painel autentica a máquina por ele).

```
::extracao {"senha": "91787170"}
::designacao {"senha": "91612540", "setor": "AUTORIZAÇÃO", "status": "Em análise", "tipo": "...", "situacao": "Aberta", "plano": "...", "regra": "...", "designada_em": "2026-10-19T16:10:29-03:00"}
```

- O valor pode ser um objeto ou uma lista de objetos. `senha` é obrigatória; na designação, `setor` também.
  Valores `null` são omitidos.
- Sem `extraida_em` / `designada_em`, vale o horário em que a API leu a linha.
- Essas linhas não entram no `log_tail`. Cada item conta em `metrics.extraidos` / `metrics.designados`;
  não misture com `::metrics` para a mesma chave.
- Item com formato inválido é descartado e o log mostra um aviso.
- Lotes de até 1000 itens, no máximo 1 requisição por segundo (o painel aceita 60/min). O envio
  acontece a cada `VM_INGESTAO_INTERVALO_SECONDS` e logo ao fim do job.
- Os itens ficam no SQLite até o painel confirmar. Painel fora do ar, `5xx`, `401` ou `429`: tenta de
  novo com espera crescente (até 5 min), inclusive depois de reiniciar a API. `422`: só os itens
  apontados pelo painel são descartados (ou o lote inteiro, se o erro não for de um item).
- `GET /api/v1/jobs/{job_id}` mostra o andamento em `ingestao: {pendentes, enviados, rejeitados}`.
- Só jobs disparados pelo painel são enviados. Execuções manuais do script não chegam ao banco.

## Persistência e reinício

Jobs ficam em SQLite (`VM_DATA_DIR/jobs.sqlite3`), então idempotência e consultas sobrevivem a
reinícios. Ao subir, jobs que estavam `RUNNING` viram `FAILED` ("API da VM foi reiniciada") e os
`QUEUED` voltam para a fila.

## Deploy

Exemplo de unidade systemd em [deploy/vm-rpa-api.service](deploy/vm-rpa-api.service). Conforme o
contrato: restringir a porta via firewall ao IP do Laravel, usar rede privada/VPN e TLS
(proxy reverso ou `--ssl-keyfile/--ssl-certfile` no uvicorn) e rotacionar o token.

VMs que só têm a porta 5000 liberada no GCP: ver [docs/migracao-porta-5000.md](docs/migracao-porta-5000.md).
