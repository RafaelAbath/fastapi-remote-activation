# Migração da conexão externa para a porta 5000

**Para:** quem for colocar a API das VMs de RPA nas demais VMs do GCP.

## Contexto

- **VM piloto (u-14):** o painel chama `https://34-95-159-3.sslip.io`. O Caddy do container
  `deploy-web-1` (projeto `buscador-credenciados`) recebe na 443 e repassa para o uvicorn em
  `172.19.0.1:8001`. Isso só funciona porque essa VM tem as portas 80/443 abertas.
- **Demais VMs:** o firewall do GCP só libera a **porta 5000**, regra herdada do sistema antigo
  (`ironman`, que chama `POST http://IP:5000/run-script/<script>`). Não podemos criar regras novas.

Duas consequências:

1. **A porta 5000 já está ocupada** pelo serviço antigo em cada VM. Ou ele sai, ou os dois passam a
   dividir a porta (Opção B).
2. **Não há certificado público.** O Let's Encrypt só valida o domínio pelas portas 80 ou 443, então
   na 5000 o HTTPS precisa de um certificado emitido por uma CA nossa, em que o painel confie.

## Qual opção usar

| Situação da VM | Opção |
|---|---|
| O `ironman` não aciona mais esta VM | **A** — uvicorn direto na 5000 com TLS (recomendada) |
| O `ironman` ainda aciona esta VM | **B** — Caddy na 5000 dividindo por caminho, em HTTP, até o `ironman` sair |

Nas duas opções, faça a restrição de origem na própria VM (seção "Restringir a origem").

## Antes de começar (em cada VM)

```bash
# Quem ocupa a 5000 hoje
sudo ss -ltnp | grep ':5000'

# Projeto instalado e funcionando localmente (ver README)
cd ~/trueAuditoria/fastapi-remote-activation
.venv/bin/pytest -q
```

Anote como o serviço antigo é iniciado (unidade systemd, cron, `screen`/`tmux`...). Esse dado é
necessário para desligá-lo (A) ou mudar a porta dele (B).

## Opção A — uvicorn na 5000 com TLS de CA própria

### 1. Criar a CA (uma vez, fora das VMs)

Faça isso numa máquina de administração. A chave da CA **não** vai para as VMs nem para o git.

```bash
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout vm-ca.key -out vm-ca.pem -subj "/CN=TrueAuditoria VM CA"
```

- `vm-ca.key`: guardar em local seguro (cofre de senhas, por exemplo).
- `vm-ca.pem`: é público e vai para o painel, que o usa para confiar nas VMs.

### 2. Emitir um certificado por VM

O certificado precisa conter o **IP externo** da VM, o mesmo cadastrado no painel.

```bash
IP=34.95.x.x   # IP externo da VM
openssl req -newkey rsa:2048 -nodes -keyout vm.key -out vm.csr -subj "/CN=$IP"
openssl x509 -req -in vm.csr -CA vm-ca.pem -CAkey vm-ca.key -CAcreateserial \
  -days 825 -sha256 -out vm.crt \
  -extfile <(printf "subjectAltName=IP:%s\nextendedKeyUsage=serverAuth" "$IP")
```

Copie `vm.key` e `vm.crt` para a VM, por exemplo em `~/trueAuditoria/fastapi-remote-activation/certs/`
(a pasta deve ser adicionada ao `.gitignore`):

```bash
chmod 600 certs/vm.key
```

Se o IP externo da VM mudar (IP efêmero), é preciso emitir um certificado novo. Prefira reservar
um IP estático para cada VM.

### 3. Desligar o serviço antigo

Use o comando que corresponde a como ele é iniciado (exemplo para systemd):

```bash
sudo systemctl disable --now <servico-antigo>
sudo ss -ltnp | grep ':5000'   # não deve listar nada
```

### 4. Subir a API na 5000

Teste manual:

```bash
.venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 5000 \
  --ssl-keyfile certs/vm.key --ssl-certfile certs/vm.crt
```

Para deixar permanente, ajuste o [deploy/vm-rpa-api.service](../deploy/vm-rpa-api.service) para o
usuário e a pasta da VM:

```ini
User=j
WorkingDirectory=/home/j/trueAuditoria/fastapi-remote-activation
EnvironmentFile=/home/j/trueAuditoria/fastapi-remote-activation/.env
ExecStart=/home/j/trueAuditoria/fastapi-remote-activation/.venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 5000 --ssl-keyfile certs/vm.key --ssl-certfile certs/vm.crt --no-server-header
```

```bash
sudo cp deploy/vm-rpa-api.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vm-rpa-api
```

A porta 5000 não precisa de root, por ser maior que 1024.

### 5. Testar de fora

```bash
curl --cacert vm-ca.pem https://$IP:5000/api/v1/health
# sem token → 401 {"detail":"Token inválido"}: TLS e roteamento OK
curl --cacert vm-ca.pem -H "Authorization: Bearer <token>" https://$IP:5000/api/v1/health
# → 200 {"status":"ok",...}
```

### 6. Painel

- **URL da API da VM:** `https://<IP externo>:5000`
- **Token:** o `VM_API_TOKEN` do `.env` da VM (um token diferente por VM)
- **O painel precisa confiar na CA.** Sem isso, toda chamada falha com erro de certificado. No
  Laravel, o cliente HTTP das VMs deve usar o `vm-ca.pem`:

  ```php
  Http::withOptions(['verify' => storage_path('certs/vm-ca.pem')])
  ```

  Essa mudança é feita no repositório do painel, uma vez para todas as VMs. **Não** use
  `verify => false`: isso desliga a proteção que o TLS dá ao token.

## Opção B — convivência com o `ironman` (temporária)

Um Caddy no host ocupa a 5000 e divide as requisições por caminho:

- `/api/v1/*` → API nova (`127.0.0.1:8001`)
- todo o resto (`/run-script/...`) → serviço antigo, movido para `127.0.0.1:5001`

O `ironman` chama em HTTP sem criptografia, e o Caddy não tem como obter certificado público na 5000.
Por isso, nesta fase a porta fica em **HTTP**, e o token trafega sem criptografia. Use essa opção só
durante a transição e migre para a Opção A assim que o `ironman` deixar de acionar a VM.

1. Mude o serviço antigo para escutar em `127.0.0.1:5001`. Isso depende de como ele foi
   configurado, que não está neste repositório.
2. Suba o uvicorn só localmente: `--host 127.0.0.1 --port 8001`, sem `--ssl-*`.
3. Instale o Caddy ([instruções oficiais para Ubuntu](https://caddyserver.com/docs/install#debian-ubuntu-raspbian))
   e use este `/etc/caddy/Caddyfile`:

   ```caddy
   {
   	auto_https off
   }

   :5000 {
   	handle /api/v1/* {
   		reverse_proxy 127.0.0.1:8001
   	}
   	handle {
   		reverse_proxy 127.0.0.1:5001
   	}
   	log
   }
   ```

   ```bash
   sudo systemctl reload caddy
   ```

4. Teste os dois caminhos de fora:

   ```bash
   curl http://$IP:5000/api/v1/health                 # 401 → API nova
   curl -X POST http://$IP:5000/run-script/inexistente  # resposta do serviço antigo
   ```

5. No painel: **URL** `http://<IP externo>:5000`.

Quando o `ironman` sair: desligue o Caddy e o serviço antigo, siga a Opção A, troque a URL no painel
para `https://` e **gere um token novo**, já que o atual trafegou sem criptografia.

## Restringir a origem (as duas opções)

Não dá para restringir no firewall do GCP, mas dá no `ufw` de cada VM, que está sob nosso controle.
Libere a 5000 só para quem precisa chamar:

```bash
sudo ufw allow OpenSSH                                    # antes de ativar, para não perder o SSH
sudo ufw allow from <IP-DO-PAINEL> to any port 5000 proto tcp
sudo ufw allow from <IP-DO-IRONMAN> to any port 5000 proto tcp   # só na Opção B
sudo ufw enable
sudo ufw status
```

O IP de saída do painel é obtido com `curl -s ifconfig.me` rodado no servidor do painel.

## Checklist por VM

- [ ] Projeto instalado e `pytest` passando
- [ ] `.env` com token próprio desta VM e `VM_SCRIPTS` apontando para os scripts reais
- [ ] Serviço antigo desligado (A) ou movido para 5001 (B)
- [ ] API respondendo na 5000 (`401` sem token, `200` com token, testados de fora)
- [ ] `ufw` restringindo a origem
- [ ] Máquina cadastrada no painel e um disparo de teste concluído
