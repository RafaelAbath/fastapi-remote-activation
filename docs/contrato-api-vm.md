# Contrato HTTP — API das VMs de RPA

**Para:** a equipe que desenvolve a FastAPI instalada em cada VM Ubuntu.
**Quem chama:** só o backend Laravel do SIAGS Flow. O navegador nunca acessa as VMs.

A API da VM é um receptor HTTP simples: valida o token, roda o script permitido como processo
local e informa o status. Não há interface gráfica, IA nem camada intermediária.

## Autenticação

Toda requisição traz `Authorization: Bearer <token da VM>`. Cada VM tem o próprio token, cadastrado
no painel (RPAs › Máquinas). Token inválido → `401`.

Recomendações da proposta: IP privado/VPN entre Laravel e VMs, TLS, firewall restrito à origem do
Laravel, rotação do token.

## Endpoints

Base: o que for cadastrado como "URL da API da VM" (ex.: `http://10.0.0.5:8000`). O Laravel acrescenta
`/api/v1/...`.

### `GET /api/v1/health`

Chamado a cada minuto. Responder rápido (timeout do Laravel: 5 s).

```json
{ "status": "ok", "version": "1.0.0", "running_jobs": 0 }
```

### `POST /api/v1/jobs`

Headers: `Authorization`, `Content-Type: application/json`, `Idempotency-Key: <execution_id>`.

```json
{
  "script": "extracao",
  "execution_id": "01a160e2-7c1b-7f6e-9d3a-2b4c5d6e7f80",
  "parameters": { "limit": 1000 }
}
```

- `script`: hoje `extracao` ou `designacao`. A VM deve manter a própria allowlist e recusar
  qualquer outro valor com `422`. **Nunca** executar comando livre vindo da requisição.
- `parameters`: só os declarados; hoje apenas `limit` (inteiro 1–10000, opcional).
- **Idempotência:** o mesmo `Idempotency-Key` repetido (retry de rede do Laravel) deve devolver o
  job já criado, não criar outro.
- Responder **`202 Accepted`** assim que o job estiver enfileirado — o script roda desacoplado da
  requisição (timeout do Laravel: 10 s).

```json
{ "job_id": "j-123", "execution_id": "01a160e2-...", "status": "QUEUED" }
```

Erros: `4xx` não é repetido pelo Laravel (vira "Erro no disparo" no painel, com o `detail` exibido);
`5xx`/timeout é repetido até 3 vezes.

### `GET /api/v1/jobs/{job_id}`

Consultado a cada minuto enquanto o job não termina.

```json
{
  "job_id": "j-123",
  "status": "RUNNING",
  "started_at": "2026-10-21T10:00:00-03:00",
  "finished_at": null,
  "metrics": { "extraidos": 120, "designados": 0, "enviados": 0 },
  "log_tail": ["10:00:01 login ok", "10:00:05 página 1/12"],
  "error": null
}
```

- `status`: `QUEUED` | `RUNNING` | `SUCCEEDED` | `FAILED` | `CANCELLED`.
- `metrics`: contagens livres; `extraidos`, `designados` e `enviados` têm rótulo no painel.
- `log_tail`: últimas linhas (o painel guarda até 200). **Sem senhas, tokens ou dados sensíveis.**
- `error`: mensagem curta quando `FAILED`.

Campos extras são ignorados e ausentes viram vazio — a API pode evoluir sem quebrar o painel.

### `POST /api/v1/jobs/{job_id}/cancel`

Pede o cancelamento → `202`. O painel mostra "Cancelando" até o `GET` devolver `CANCELLED`.

## Erros

Corpo JSON com `detail` (padrão FastAPI) ou `error`; o texto aparece para o usuário.
