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

- `script`: hoje `extracao`, `designacao`, `envio_aria`, `previa_envio` ou `conferencia_aria`. A VM deve manter a própria allowlist
  (`VM_SCRIPTS`) e recusar qualquer outro valor com `422`. **Nunca** executar comando livre vindo
  da requisição.
- `parameters`: só os declarados por script, todos opcionais. O painel nunca manda outros.

  | Script | Parâmetro | Valores | Na VM |
  |---|---|---|---|
  | `extracao`, `designacao` | `limit` | inteiro 1–10000 | limite do script |
  | `envio_aria` | `lote` | `opme`, `prazo`, `prioritarias`, `odonto` | `--lote <valor>`; ausente = todos os lotes |
  | `envio_aria` | `sem_aria` | `true` (só vem quando ligado) | `--sem-aria`: só separa os lotes, não envia |
  | `conferencia_aria` | `data` | `AAAA-MM-DD`, até hoje | `--data <valor>`; ausente = hoje |
  | `conferencia_aria` | `lote` | `opme`, `prazo`, `prioritarias`, `odonto` | `--lote <valor>`; ausente = todos |
  | `conferencia_aria` | `sem_siags` | `true` (só vem quando ligado) | `--sem-siags`: não consulta o SIAGS (usa só o status da Aria) |

  `previa_envio` é a **prévia do envio** (novo em 19/11/2026, **a VM precisa incluir na `VM_SCRIPTS`**):
  roda `envio_aria.py` **sem** `--enviar` e **sem** `--sem-aria`, sem parâmetros. Monta os lotes, consulta o
  histórico da Aria (avisa lote já criado hoje) e gera o relatório do envio (que vai ao painel pelo
  `POST /api/rpa/ingestao/relatorios-envio`), mas **não cria nada na Aria**. Liberado para todos (nível 5):
  o botão "Gerar prévia do envio" da tela de Início dispara este script. Enquanto a VM não o conhecer, o
  disparo volta `422` e a execução fica em `ERRO_DISPARO`.

  `conferencia_aria` é o **Relatório Aria** (`conferencia_aria.py`): só lê a Aria e o SIAGS e grava a
  aba Conferência Aria e `POST /api/rpa/ingestao/conferencias-aria`. Liberado para todos (nível 5).
  Imprime `::metrics` com os totais por resultado (`resolvida`, `aguarda_equipe`, `erro_lancamento`,
  `aguarda_lancamento`, `nao_processada`, `nao_aceita`, `aguardando`, `so_na_aria`, `enviadas`).

  `envio_aria` **sem** `sem_aria` é envio de verdade: a VM roda `envio_aria.py --enviar
  --sem-confirmacao` (mais `--lote`, se vier). No painel, só Gestor ou acima consegue disparar
  esse script.
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
- `started_at` / `finished_at`: ISO 8601 **com fuso** — `2026-10-21T17:47:15-03:00` ou
  `2026-10-21T20:47:15Z`. Sem fuso, o painel assume `America/Sao_Paulo` (configurável em
  `RPA_FUSO_VM`), mas não conte com isso: envie sempre o offset.
- `metrics`: contagens livres; `extraidos`, `designados` e `enviados` têm rótulo no painel.
- `log_tail`: últimas linhas (o painel guarda até 200). **Sem senhas, tokens ou dados sensíveis.**
- `error`: mensagem curta quando `FAILED`.

Campos extras são ignorados e ausentes viram vazio — a API pode evoluir sem quebrar o painel.

### `POST /api/v1/jobs/{job_id}/cancel`

Pede o cancelamento → `202`. O painel mostra "Cancelando" até o `GET` devolver `CANCELLED`.

## Erros

Corpo JSON com `detail` (padrão FastAPI) ou `error`; o texto aparece para o usuário.

## Envio de resultados ao painel (VM → Laravel)

Além de responder ao painel, a VM **envia** as senhas que os scripts produzem. A planilha do Google
continua sendo escrita em paralelo durante a transição; o envio ao painel é adicional.

- **URL:** `<URL do painel>/api/rpa/ingestao/extracoes`, `.../designacoes` e `.../envios-aria` (`POST`, JSON).
  Em desenvolvimento, com o Laravel local, a VM chega nele por túnel SSH reverso — ver
  [runbook-tunel-reverso-vm.md](runbook-tunel-reverso-vm.md).
- **Autenticação:** `Authorization: Bearer <token da VM>` — o **mesmo** token cadastrado para a
  máquina no painel. Máquina desativada ou token desconhecido → `401`.
- **Rodada:** informe `execution_id` (quando o job veio do painel — é o mesmo recebido no
  `POST /jobs`) **ou** `execucao_externa` (quando o script rodou por fora, ex.
  `20261019_161014_7c5b2f6d`, o valor da coluna EXECUCAO da planilha).
- **Lotes:** até **1000** itens por requisição. Pode (e deve) enviar durante a execução, lote a lote.
- **Reenvio é seguro:** a mesma senha na mesma rodada não duplica — reenvie o lote inteiro se a
  resposta não chegar.
- **Datas:** ISO 8601 com fuso (`-03:00` ou `Z`). Sem fuso, vale `America/Sao_Paulo`. Sem data,
  vale o momento do recebimento.
- **Limite:** 60 requisições/min por máquina.

### `POST /api/rpa/ingestao/extracoes`

```json
{
  "execucao_externa": "20261022_212457_ab12",
  "itens": [
    { "senha": "91787170", "extraida_em": "2026-10-22T21:24:57-03:00", "origem": "SIAGS" },
    { "senha": "91788750", "extraida_em": "2026-10-22T21:24:57-03:00", "origem": "PLANILHA" }
  ]
}
```

- `origem` (opcional, 19/11/2026): `SIAGS` (filtro SADT) ou `PLANILHA` (Controle SIAGS sem setor, que o
  filtro não traz). Ausente vale `SIAGS`. O painel de Início mostra SIAGS × planilha por rodagem.

**Métricas de progresso (painel de Início).** Durante a rodagem, os scripts imprimem `::metrics` e a API da
VM devolve esses números em `metrics` no `GET /jobs/{id}`, como já faz:

| Script | Chaves | Uso no painel |
|---|---|---|
| `extracao` | `siags`, `planilha`, `siags_informado`, `pagina`, `paginas_total` | "página X de Y" e a barra do card |
| `designacao` | `a_designar`, `designadas` | "X de Y designadas" e a barra do card |

### `POST /api/rpa/ingestao/designacoes`

```json
{
  "execution_id": "01a160e2-7c1b-7f6e-9d3a-2b4c5d6e7f80",
  "itens": [
    {
      "senha": "91612540",
      "setor": "AUTORIZAÇÃO",
      "status": "Em análise",
      "tipo": "WEB - RESUMO DE INTERNACAO",
      "situacao": "Aberta",
      "plano": "PLANO SAÚDE",
      "regra": "AUTORIZAÇÃO padrão | 2ª passada: sem termo",
      "designada_em": "2026-10-19T16:10:29-03:00"
    }
  ]
}
```

`setor` é o texto da coluna SETOR da planilha. O painel casa sem acento/maiúsculas com o catálogo
(AUTORIZAÇÃO, MEDICAMENTO, OPME, FACEC.OPME, HOME CARE, ODONTO); setor desconhecido é aceito,
contado como "Outros", e o texto original é guardado. Só `senha` e `setor` são obrigatórios.

### `POST /api/rpa/ingestao/envios-aria`

```json
{
  "execution_id": "01a160e2-7c1b-7f6e-9d3a-2b4c5d6e7f80",
  "itens": [
    { "senha": "91612540", "lote": "PRAZO", "auditoria_id": "3681", "enviada_em": "2026-10-31T10:12:00-03:00" }
  ]
}
```

Uma linha por senha de cada auditoria criada na Aria. Só `senha` é obrigatória.

### `POST /api/rpa/ingestao/conferencias-aria`

Enviado pelo `conferencia_aria.py` (designacao-automacao): para cada senha de um lote do dia, se a
Aria aceitou a senha na auditoria. Roda depois do envio, pelo terminal ou pelo agente, então a rodada
é `execucao_externa` (a da conferência).

```json
{
  "execucao_externa": "20261107_150000_ab12",
  "itens": [
    { "senha": "92895520", "dia": "2026-11-07", "lote": "ODONTO", "situacao": "ACEITA",
      "decisao_aria": "SOLICITAR_COMPLEMENTO", "status_aria": "APROVADA",
      "auditoria_id": "9621f2bd-0b87-4d4d-8000-6468cbc261ef", "auditoria_nome": "ODONTO (15)",
      "conferida_em": "2026-11-07T15:00:00-03:00" },
    { "senha": "91612540", "dia": "2026-11-07", "lote": "PRAZO", "situacao": "AGUARDANDO" }
  ]
}
```

- Obrigatórios: `senha`, `dia` (`AAAA-MM-DD`, o dia do envio), `lote` e `situacao`.
- `situacao`: `ACEITA` (enviada e na auditoria), `NAO_ACEITA` (enviada e fora da auditoria concluída),
  `ERRO_ARIA` (enviada e fora de uma auditoria que a Aria parou em `ERRO`, `CANCELADO` ou `FALHOU`: não
  processada, não recusada), `AGUARDANDO` (enviada e ainda fora da auditoria, que não terminou: a Aria lista as guias à medida que analisa) ou `SO_NA_ARIA`
  (na auditoria sem registro do robô naquele dia e lote).
- **Diferente das outras rotas, reenviar substitui:** a chave é (senha, dia, lote) e a linha é
  atualizada com o que chegar por último (AGUARDANDO vira ACEITA ou NAO_ACEITA na próxima
  conferência). Na resposta, `ja_existentes` conta as linhas atualizadas.
- `senhas.situacao_aria`, `decisao_aria` e `conferida_aria_em` guardam a conferência do dia de envio
  mais recente da senha (aparecem na consulta de situação); `senhas.resultado_aria`, o resultado dela.
- Campos do **Relatório Aria**, todos opcionais (lotes sem eles continuam válidos):

  | Campo | Conteúdo |
  |---|---|
  | `resultado` | `AGUARDA_EQUIPE`, `ERRO_LANCAMENTO`, `NAO_PROCESSADA`, `NAO_ACEITA`, `AGUARDANDO`, `AGUARDA_LANCAMENTO`, `SO_NA_ARIA` ou `RESOLVIDA` |
  | `bloqueio_envio` | se a Aria bloqueou o lançamento do parecer no SIAGS (relatório administrativo) |
  | `motivo_bloqueio_codigo` | `DECISAO_PROTEGIDA`, `COMPLEMENTO_JA_ABERTO` (as duas = já resolvidas), `OPME_SEM_DECISAO_AUTOMATICA`, `ANALISE_INCOMPLETA`, `ADM_REVERSAO_SEM_PERFIL`, `PARECER_COM_MARCADOR`, `CANCELAMENTO_SEM_PROVA`, `OPME_TRATADA_POR_HUMANO` ou `OUTRO` |
  | `motivo_bloqueio` | texto da Aria (até 300) |
  | `status_aria` | etapa do lançamento: `PROCESSADA`, `PENDENTE_REVISAO`, `APROVADA`, `ENVIADA`, `ERRO_ENVIO` |
  | `status_siags_aria` | status no SIAGS segundo a Aria |
  | `status_siags`, `siags_consultado_em` | status consultado pelo robô no SIAGS (todas as senhas, salvo `sem_siags`) |
  | `enviado_siags_em` | quando a Aria lançou o parecer no SIAGS |

### `POST /api/rpa/ingestao/relatorios-envio`

O relatório do envio do dia (o mesmo da aba **Relatório Envio**), já estruturado. Mandado pelo
`relatorio_aria.py` ao fim de cada rodagem do envio (prévia ou real). **Um por dia:** reenviar o mesmo
`dia` troca o anterior, os outros dias ficam. Sem `itens`: o corpo é o próprio relatório.

```json
{
  "execucao_externa": "20261119_123143_9f2a",
  "dia": "2026-11-19",
  "gerado_em": "2026-11-19T12:31:43-03:00",
  "grupos": [
    { "codigo": "prazo", "nome": "PRAZO", "aptas": 1650, "total": 1838,
      "fora": [{ "motivo": "#ADM", "total": 105 }, { "motivo": "Autorizada", "total": 74 }] },
    { "codigo": "prioritarias", "nome": "PRIORITÁRIAS", "aptas": 25, "total": 27,
      "prazos": [{ "prazo": "Vence Hoje", "total": 2 }, { "prazo": "Vencido", "total": 19 }],
      "fora": [{ "motivo": "#ADM", "total": 1 }],
      "senhas_restricao": [{ "senha": "91096010", "prazo": "Vencido", "restricao": "#ADM" }] }
  ],
  "manha": { "total": 27, "na_manha": 21, "entraram": 6, "aviso": null,
    "sem_marcacao": [{ "senha": "93501740", "prazo": "Vence Hoje" }],
    "com_marcacao": [{ "senha": "91096010", "prazo": "Vencido", "marcacao": "#ADM" }] },
  "lotes": { "prazo": { "status": "PREVIA", "texto": "prévia (não enviado)" } },
  "mensagens": { "prazo": "*PRAZO — 19/11*\n\nSerão enviadas para a ARIA: *1650/1838*" }
}
```

| Campo | Regra |
|---|---|
| `grupos[].codigo` | `prioritarias`, `prazo`, `opme`, `odonto` (sem repetir) |
| `grupos[].fora` | motivos de quem ficou de fora (nas prioritárias, as restrições) |
| `lotes.<grupo>.status` | `PREVIA`, `ENVIADO`, `JA_CRIADO` (já havia auditoria do lote hoje), `FALHA`, `MISTO` |
| `mensagens` | texto original de cada coluna da aba (`odonto`, `prioritarias`, `prazo`, `opme`, `manha`), para o "Copiar mensagem" |

Resposta: `200` com `{ "dia": "2026-11-19", "substituido": false }`.

### Resposta (as rotas com `itens`)

`200` com `{ "recebidos": 2, "novos": 2, "ja_existentes": 0 }`. Erro de formato → `422` com a
lista de campos inválidos; nada do lote é gravado.

## Consulta de situação das senhas (VM → Laravel)

Usada pelo `envio_aria.py` para não reenviar à Aria, dentro da janela `ENVIO_REENVIO_DIAS`, as senhas
que o painel sabe que já foram enviadas. A designação não consulta mais o painel: roda toda a extração.

- **URL:** `POST <URL do painel>/api/rpa/senhas/situacao` — com `PAINEL_API_URL` apontando para
  `https://siags-flow.onrender.com/api/rpa`, o script chama `{PAINEL_API_URL}/senhas/situacao`.
- **Autenticação:** `Authorization: Bearer <token da VM>` — o **mesmo** token da ingestão
  (`PAINEL_API_TOKEN` = token cadastrado para a máquina). Inválido/inativa → `401`.
- **Lotes:** até **1000** senhas por requisição. Limite de 60 requisições/min por máquina (o mesmo
  da ingestão).

```json
{ "senhas": ["92063720", "92061830", "00000000"] }
```

Resposta `200` — um objeto com **toda** senha pedida como chave:

```json
{
  "92063720": { "designada_em": "2026-10-28T15:19:29-03:00", "setor": "FACEC.OPME", "enviada_aria_em": null,
                "situacao_aria": null, "decisao_aria": null, "conferida_aria_em": null },
  "92895520": { "designada_em": null, "setor": null, "enviada_aria_em": "2026-11-07T12:19:41-03:00",
                "situacao_aria": "ACEITA", "decisao_aria": "SOLICITAR_COMPLEMENTO",
                "conferida_aria_em": "2026-11-07T15:00:00-03:00" },
  "00000000": { "designada_em": null, "setor": null, "enviada_aria_em": null,
                "situacao_aria": null, "decisao_aria": null, "conferida_aria_em": null }
}
```

- `designada_em`: a designação **mais recente** que o painel recebeu para a senha, em ISO 8601 no
  horário de Brasília. `null` = o painel não tem designação dela (só extraída, ou desconhecida).
- `setor`: o texto da coluna SETOR enviado naquela designação.
- `enviada_aria_em`: o envio à Aria **mais recente** que o painel recebeu para a senha, em ISO 8601 no
  horário de Brasília. `null` = o painel não tem envio dela. Envios feitos fora do painel não aparecem.
- `situacao_aria`, `decisao_aria`, `conferida_aria_em`: a conferência do envio mais recente
  (`POST /api/rpa/ingestao/conferencias-aria`). `null` = a senha nunca foi conferida.
- Senhas repetidas no pedido aparecem uma vez; espaços nas pontas são removidos.
- Erro de formato (lista vazia, mais de 1000, número inválido) → `422`.
- O painel só conhece o que a VM enviou pela ingestão: designações feitas antes de a ingestão
  existir não aparecem (ver [pendencias.md](pendencias.md)).
