# MeLi Faturamento Sync

## 1. Visao Geral

Servico que sincroniza o faturamento diario de contas do MercadoLivre com o Supabase. A cada N minutos (padrao: 5), consulta a API de pedidos pagos de cada conta MeLi configurada, soma o valor total do dia (BRT) e faz upsert na tabela `faturamento` do Supabase (projeto 141Air). O dashboard React consome essa tabela via real-time subscription.

**Resumo do fluxo:**
1. O servico inicia e auto-descobre contas MeLi a partir de variaveis de ambiente
2. Carrega os refresh tokens mais recentes do Supabase (tabela `meli_tokens`)
3. A cada ciclo de sync: troca refresh_token por access_token, busca pedidos pagos do dia, soma valores, faz upsert na tabela `faturamento`
4. O dashboard recebe a atualizacao em tempo real

---

## 2. Stack Tecnologico

| Componente     | Tecnologia                   |
|----------------|------------------------------|
| Linguagem      | Python 3.12                  |
| Framework      | FastAPI 0.115.0              |
| HTTP Client    | httpx 0.27.0                 |
| ASGI Server    | uvicorn 0.30.0               |
| Env vars       | python-dotenv 1.0.1          |
| Banco de dados | Supabase (PostgREST)         |
| Deploy         | Docker via EasyPanel         |
| API externa    | MercadoLivre API v1          |

---

## 3. Estrutura do Projeto

```
meli-faturamento-sync/
  main.py             # Toda a logica do servico (arquivo unico)
  requirements.txt    # Dependencias Python
  Dockerfile          # Build image (python:3.12-slim)
  .env                # Credenciais (NAO versionado)
  .env.example        # Template de variaveis de ambiente
  .gitignore          # Ignora .env, __pycache__, venv
```

O servico e um **arquivo unico** (`main.py`) com ~413 linhas. Nao ha separacao em modulos/services.

---

## 4. Configuracao - Variaveis de Ambiente

### Variaveis Globais

| Variavel                 | Obrigatoria | Default | Descricao                                      |
|--------------------------|-------------|---------|------------------------------------------------|
| `SYNC_INTERVAL_MINUTES`  | Nao         | `5`     | Intervalo entre syncs automaticos (minutos)    |
| `LOG_LEVEL`              | Nao         | `INFO`  | Nivel de log (DEBUG, INFO, WARNING, ERROR)     |
| `SUPABASE_URL`           | Sim         | -       | URL do projeto Supabase (141Air)               |
| `SUPABASE_SERVICE_KEY`   | Sim         | -       | Service role key do Supabase (acesso total)    |

**Valores fixos:**
- `SUPABASE_URL` = `https://iezxmhrjndzuckjcxihd.supabase.co`
- O Supabase usado e o projeto **141Air** (ref: `iezxmhrjndzuckjcxihd`)

### Variaveis por Conta MeLi

Para cada conta MeLi, o padrao de nomenclatura e `MELI_{NAME}_*` onde `{NAME}` e um identificador em MAIUSCULAS (ex: `141`, `NETAIR`, `NETPARTS`):

| Variavel                      | Obrigatoria | Descricao                                          |
|-------------------------------|-------------|-----------------------------------------------------|
| `MELI_{NAME}_APP_ID`          | Sim         | Application ID do app MeLi Developer                |
| `MELI_{NAME}_SECRET_KEY`      | Sim         | Secret key do app MeLi Developer                    |
| `MELI_{NAME}_REFRESH_TOKEN`   | Sim         | Refresh token inicial (seed). Depois o servico usa o do Supabase |
| `MELI_{NAME}_USER_ID`         | Sim         | User ID do seller no MercadoLivre                   |
| `ACCOUNT_{NAME}_EMPRESA`      | Nao         | Nome da empresa no dashboard. Default = `{NAME}`    |

**IMPORTANTE:** O `MELI_{NAME}_REFRESH_TOKEN` na env var e usado APENAS como seed inicial. Apos o primeiro sync bem-sucedido, o servico salva o token atualizado no Supabase e passa a usar esse. O token na env var nao precisa ser atualizado manualmente depois.

### Contas Atuais

| NAME       | User ID       | Empresa (Dashboard) | APP_ID           |
|------------|---------------|----------------------|------------------|
| `141`      | `1963376627`  | `141AIR`             | Definido no .env |
| `NETAIR`   | `421259712`   | `NETAIR`             | Definido no .env |
| `NETPARTS` | `1092904133`  | `NETPARTS`           | Definido no .env |

---

## 5. Endpoints da API

O servico FastAPI expoe 4 endpoints. Nenhum requer autenticacao (servico interno).

### GET /

**Descricao:** Retorna informacoes basicas do servico.

**Response 200:**
```json
{
  "service": "meli-faturamento-sync",
  "accounts": ["141AIR", "NETAIR", "NETPARTS"],
  "interval_minutes": 5,
  "last_sync": "2025-07-16T14:30:00-03:00"
}
```

- `accounts`: lista dos nomes de empresa de cada conta configurada
- `last_sync`: timestamp ISO do ultimo sync (null se nunca executou)

---

### GET /health

**Descricao:** Health check para monitoramento.

**Response 200:**
```json
{
  "status": "healthy",
  "accounts": 3,
  "last_sync": "2025-07-16T14:30:00-03:00"
}
```

---

### POST /sync

**Descricao:** Dispara um sync manual (mesmo comportamento do sync agendado).

**Response 200:**
```json
{
  "results": [
    {
      "empresa": "141AIR",
      "date": "2025-07-16",
      "valor": 15230.50,
      "orders": 42,
      "fraud_skipped": 1,
      "status": "synced"
    },
    {
      "empresa": "NETAIR",
      "date": "2025-07-16",
      "valor": 0,
      "orders": 0,
      "fraud_skipped": 0,
      "status": "no_sales"
    },
    {
      "empresa": "NETPARTS",
      "date": "2025-07-16",
      "status": "token_error"
    }
  ]
}
```

**Status possiveis por conta:**
| Status          | Significado                                         |
|-----------------|-----------------------------------------------------|
| `synced`        | Dados sincronizados com sucesso no Supabase         |
| `no_sales`      | Nenhuma venda paga no dia (valor = 0), nao faz upsert |
| `token_error`   | Falha ao obter access_token (refresh expirado?)     |
| `upsert_error`  | Pedidos obtidos mas falha ao gravar no Supabase     |
| `error`         | Excecao inesperada (campo `error` contem mensagem)  |

---

### GET /last

**Descricao:** Retorna os resultados do ultimo sync executado (sem disparar novo).

**Response 200:**
```json
{
  "last_sync": "2025-07-16T14:30:00-03:00",
  "results": [
    {
      "empresa": "141AIR",
      "date": "2025-07-16",
      "valor": 15230.50,
      "orders": 42,
      "fraud_skipped": 1,
      "status": "synced"
    }
  ]
}
```

---

## 6. Fluxo de Dados - Como o Sync Funciona

### Sequencia completa de um ciclo de sync:

```
1. sync_all() e chamado (pelo scheduler ou via POST /sync)
   |
2. Determina a data atual em BRT (timezone -03:00)
   |
3. Para cada conta em ACCOUNTS (sequencialmente):
   |
   3a. _get_access_token(account, client)
   |    - POST https://api.mercadolibre.com/oauth/token
   |    - grant_type=refresh_token
   |    - Envia: client_id, client_secret, refresh_token
   |    - Recebe: access_token (6h) + NOVO refresh_token
   |    - Atualiza refresh_token em memoria (account dict)
   |    - Salva no Supabase (tabela meli_tokens) via upsert
   |    - Se falhar: status = "token_error", pula conta
   |
   3b. _fetch_paid_orders_total(account, access_token, date_str, client)
   |    - GET /orders/search com filtros de data e status=paid
   |    - Pagina com offset (0, 50, 100...) ate cobrir todos os resultados
   |    - Limite de seguranca: offset max 500 (10 paginas)
   |    - Ignora pedidos com tag "fraud_risk_detected"
   |    - Usa paid_amount (inclui frete); fallback para total_amount
   |    - Retorna: { valor, order_count, fraud_skipped }
   |
   3c. Se valor > 0:
   |    _upsert_faturamento(empresa, date_str, valor, client)
   |    - POST /rest/v1/faturamento?on_conflict=empresa,data
   |    - Header: Prefer: resolution=merge-duplicates,return=minimal
   |    - Grava/atualiza o faturamento do dia para a empresa
   |
   3d. Se valor == 0: status = "no_sales" (nao grava nada)
   |
4. Retorna lista de resultados de todas as contas
   |
5. O dashboard React recebe a atualizacao via Supabase real-time
```

### Scheduler (loop em background):

```
1. Na inicializacao (lifespan):
   - Cria asyncio.Task com _scheduler()

2. _scheduler():
   - Carrega tokens do Supabase (_load_all_tokens)
   - Executa sync_all() imediatamente
   - Loop infinito: sleep(SYNC_INTERVAL * 60) → sync_all()

3. No shutdown:
   - A task e cancelada
```

---

## 7. MeLi API - Endpoints Utilizados

### POST /oauth/token

**URL:** `https://api.mercadolibre.com/oauth/token`

**Content-Type:** `application/x-www-form-urlencoded`

**Parametros (grant_type=refresh_token):**
| Campo          | Valor                                |
|----------------|--------------------------------------|
| `grant_type`   | `refresh_token`                      |
| `client_id`    | APP_ID da conta                      |
| `client_secret`| SECRET_KEY da conta                  |
| `refresh_token`| Ultimo refresh_token valido          |

**Parametros (grant_type=authorization_code):**
| Campo          | Valor                                |
|----------------|--------------------------------------|
| `grant_type`   | `authorization_code`                 |
| `client_id`    | APP_ID da conta                      |
| `client_secret`| SECRET_KEY da conta                  |
| `code`         | Codigo obtido via fluxo OAuth        |
| `redirect_uri` | URI configurada no MeLi Developer    |

**Response 200:**
```json
{
  "access_token": "APP_USR-...",
  "token_type": "Bearer",
  "expires_in": 21600,
  "scope": "offline_access read write",
  "user_id": 1963376627,
  "refresh_token": "TG-..."
}
```

---

### GET /orders/search

**URL:** `https://api.mercadolibre.com/orders/search`

**Headers:**
```
Authorization: Bearer {access_token}
```

**Query Parameters:**
| Param                       | Valor                              | Descricao                      |
|-----------------------------|-------------------------------------|--------------------------------|
| `seller`                    | User ID do seller                  | Filtra por vendedor            |
| `order.status`              | `paid`                             | Apenas pedidos pagos           |
| `order.date_created.from`   | `2025-07-16T00:00:00.000-03:00`    | Inicio do dia (BRT)           |
| `order.date_created.to`     | `2025-07-16T23:59:59.999-03:00`    | Fim do dia (BRT)              |
| `sort`                      | `date_desc`                        | Mais recentes primeiro         |
| `limit`                     | `50`                               | Resultados por pagina (max 50) |
| `offset`                    | `0`, `50`, `100`...                | Paginacao                      |

**Response 200:**
```json
{
  "paging": {
    "total": 87,
    "offset": 0,
    "limit": 50
  },
  "results": [
    {
      "id": 123456789,
      "status": "paid",
      "total_amount": 150.00,
      "paid_amount": 189.90,
      "currency_id": "BRL",
      "date_created": "2025-07-16T10:30:00.000-03:00",
      "date_closed": "2025-07-16T10:31:00.000-03:00",
      "tags": ["paid", "not_delivered"]
    }
  ]
}
```

**Campos usados pelo servico:**
| Campo          | Uso                                                   |
|----------------|-------------------------------------------------------|
| `paid_amount`  | Valor principal. Inclui produto + frete (total pago)  |
| `total_amount` | Fallback se paid_amount for null/0. So produto.       |
| `tags`         | Se contem `"fraud_risk_detected"`, pedido e ignorado  |

**Paginacao:** O servico incrementa offset em 50 ate `offset >= total` ou `offset > 500` (limite de seguranca = max 500 pedidos/dia por conta).

---

## 8. Token Lifecycle - Refresh Token de Uso Unico

O refresh token do MercadoLivre tem um comportamento critico que diferencia de outras APIs:

### Regras fundamentais:

1. **Refresh token e de USO UNICO** -- apos ser usado em um POST /oauth/token, aquele token e INVALIDADO permanentemente
2. A resposta do POST /oauth/token SEMPRE traz um NOVO refresh_token
3. O novo refresh_token DEVE ser salvo imediatamente -- se perdido, a conta perde acesso
4. Access token dura 6 horas
5. Refresh token expira em 6 meses se nao usado

### Fluxo no servico:

```
[Startup]
   |
   v
_load_all_tokens()
   - Para cada conta, busca refresh_token da tabela meli_tokens no Supabase
   - Se encontra: usa esse (mais recente que o da env var)
   - Se nao encontra: usa o da env var (seed inicial)
   |
   v
[Cada ciclo de sync]
   |
   v
_get_access_token(account)
   - POST /oauth/token com refresh_token atual
   - Recebe: access_token + NOVO refresh_token
   - Atualiza em memoria: account["refresh_token"] = novo_token
   - Salva no Supabase: _save_token_to_db()
   |
   v
[Usa access_token para buscar pedidos]
```

### Cenarios de falha:

| Cenario                          | Causa                           | Solucao                              |
|----------------------------------|---------------------------------|--------------------------------------|
| `token_error` no sync            | Refresh token expirado/invalido | Gerar novo via fluxo OAuth (secao 11)|
| Token salvo no Supabase invalido | Servico crashou entre usar e salvar | Idem acima                       |
| Token da env var usado 2x        | Servico reiniciou sem token no DB   | Normal na primeira vez, problema se ja foi usado |

---

## 9. Supabase - Tabelas e Operacoes

**Projeto:** 141Air
**Ref:** `iezxmhrjndzuckjcxihd`
**URL:** `https://iezxmhrjndzuckjcxihd.supabase.co`

### Headers padrao (PostgREST):

```
apikey: {SUPABASE_SERVICE_KEY}
Authorization: Bearer {SUPABASE_SERVICE_KEY}
Content-Type: application/json
```

### Tabela: `faturamento`

| Coluna   | Tipo     | Constraint                     | Descricao                     |
|----------|----------|--------------------------------|-------------------------------|
| `empresa`| TEXT     | UNIQUE(empresa, data)          | Nome da empresa (ex: 141AIR)  |
| `data`   | DATE     | UNIQUE(empresa, data)          | Data do faturamento (BRT)     |
| `valor`  | NUMERIC  | -                              | Total faturado no dia (R$)    |

**Operacao: Upsert faturamento**
```
POST /rest/v1/faturamento?on_conflict=empresa,data
Headers:
  Prefer: resolution=merge-duplicates,return=minimal
Body:
  {"empresa": "141AIR", "data": "2025-07-16", "valor": 15230.50}
```

Se ja existe registro para (empresa, data), atualiza o valor. Se nao existe, insere.

### Tabela: `meli_tokens`

| Coluna                   | Tipo      | Constraint   | Descricao                              |
|--------------------------|-----------|--------------|----------------------------------------|
| `account_name`           | TEXT      | PRIMARY KEY  | Nome da conta (ex: `141`, `NETAIR`)    |
| `refresh_token`          | TEXT      | -            | Ultimo refresh_token valido            |
| `access_token`           | TEXT      | -            | Ultimo access_token (informativo)      |
| `access_token_expires_at`| TIMESTAMP | -           | Quando o access_token expira           |
| `updated_at`             | TIMESTAMP | -           | Quando o registro foi atualizado       |

**Operacao: Load token**
```
GET /rest/v1/meli_tokens?account_name=eq.141&select=refresh_token
```

**Operacao: Save/upsert token**
```
POST /rest/v1/meli_tokens
Headers:
  Prefer: resolution=merge-duplicates
Body:
  {
    "account_name": "141",
    "refresh_token": "TG-...",
    "access_token": "APP_USR-...",
    "access_token_expires_at": "2025-07-16T20:30:00-03:00",
    "updated_at": "2025-07-16T14:30:00-03:00"
  }
```

---

## 10. Mapeamento de Contas

O mapeamento entre conta MeLi e empresa no dashboard e feito pela variavel `ACCOUNT_{NAME}_EMPRESA`.

```
Variavel de Ambiente             Conta MeLi (NAME)    Empresa (Dashboard)
------------------------------   ------------------   --------------------
MELI_141_*                       141                  141AIR
MELI_NETAIR_*                    NETAIR               NETAIR
MELI_NETPARTS_*                  NETPARTS             NETPARTS
```

### Auto-descoberta de contas:

A funcao `_load_accounts()` varre TODAS as variaveis de ambiente buscando o padrao `MELI_{NAME}_APP_ID`. Para cada match:
1. Extrai o `{NAME}` (tudo entre `MELI_` e `_APP_ID`)
2. Busca as outras 3 variaveis obrigatorias: `_SECRET_KEY`, `_REFRESH_TOKEN`, `_USER_ID`
3. Busca a variavel opcional `ACCOUNT_{NAME}_EMPRESA` (default: o proprio NAME)
4. Se todas as 4 obrigatorias existem, adiciona a conta

**Isso significa que para adicionar uma nova conta, basta adicionar as env vars e reiniciar o servico. Nao e necessario alterar codigo.**

### Como o dashboard usa o campo `empresa`:

O dashboard React le a tabela `faturamento` e agrupa por `empresa`. O nome que aparece no dashboard e exatamente o valor de `ACCOUNT_{NAME}_EMPRESA`. Por isso:
- `ACCOUNT_141_EMPRESA=141AIR` faz o faturamento da conta 141 aparecer como "141AIR" no dashboard
- Se a variavel nao existir, usaria "141" (o NAME) como nome da empresa

---

## 11. Guia para LLMs: Como Incluir e Excluir Contas MeLi

**ATENCAO: Esta secao foi escrita especificamente para que uma LLM consiga executar cada passo de forma autonoma e inequivoca.**

---

### 11.1 Incluir Nova Conta MeLi

**Pre-requisitos:**
- Acesso ao MeLi Developer Portal (https://developers.mercadolivre.com.br/devcenter)
- Acesso ao `.env` do servico (ou variaveis de ambiente no EasyPanel)
- Acesso ao Supabase (via SQL Editor ou API)
- O dono da conta MeLi precisa autorizar o app (passo interativo no navegador)

#### Passo 1: Definir o NAME da conta

Escolha um identificador em MAIUSCULAS, sem espacos, sem caracteres especiais. Exemplos: `NOVAEMPRESA`, `LOJA2`, `BRAND_X`.

Este NAME sera usado em todas as variaveis de ambiente.

#### Passo 2: Criar o App no MeLi Developer Portal (se necessario)

Se a nova conta pertence a um app MeLi ja existente, pule para o Passo 3. Caso contrario:

1. Acesse https://developers.mercadolivre.com.br/devcenter
2. Crie um novo aplicativo
3. Anote o `APP_ID` e `SECRET_KEY`
4. Configure o `redirect_uri` (ex: `https://seusite.com/callback` ou `https://httpbin.org/get` para testes)
5. Ative os escopos: `read`, `write`, `offline_access`

**IMPORTANTE:** O `redirect_uri` DEVE ser exatamente o mesmo em todos os lugares (Developer Portal, URL de autorizacao, POST /oauth/token). Qualquer diferenca causa erro.

#### Passo 3: Obter o Authorization Code (interativo)

O dono da conta MeLi precisa abrir esta URL no navegador:

```
https://auth.mercadolivre.com.br/authorization?response_type=code&client_id={APP_ID}&redirect_uri={REDIRECT_URI}
```

**Exemplo concreto:**
```
https://auth.mercadolivre.com.br/authorization?response_type=code&client_id=225841712681336&redirect_uri=https://httpbin.org/get
```

Apos autorizar, o navegador redireciona para:
```
{REDIRECT_URI}?code=TG-XXXXXXXXXXXXXXXXXX-XXXXXXXXXX
```

**Copie o valor do parametro `code`.** Ele expira em 10 minutos.

#### Passo 4: Trocar o Code por Tokens

Execute este curl (substituindo os valores):

```bash
curl -X POST https://api.mercadolibre.com/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id={APP_ID}" \
  -d "client_secret={SECRET_KEY}" \
  -d "code={CODE_DO_PASSO_3}" \
  -d "redirect_uri={REDIRECT_URI}"
```

**Exemplo concreto:**
```bash
curl -X POST https://api.mercadolibre.com/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id=225841712681336" \
  -d "client_secret=ZhPjCrT5XXXXXXXXXXXXXXXXXXX" \
  -d "code=TG-669a86724f1ed40001f050e8-1963376627" \
  -d "redirect_uri=https://httpbin.org/get"
```

**Response esperada:**
```json
{
  "access_token": "APP_USR-225841712681336-...",
  "token_type": "Bearer",
  "expires_in": 21600,
  "scope": "offline_access read write",
  "user_id": 1963376627,
  "refresh_token": "TG-XXXXXXXXXX-XXXXXXXXXX"
}
```

**Anote o `refresh_token` e o `user_id`.**

#### Passo 5: Obter o User ID do seller

O `user_id` retornado no passo 4 e o user ID do seller. Alternativamente, voce pode consultar:

```bash
curl -H "Authorization: Bearer {ACCESS_TOKEN}" \
  https://api.mercadolibre.com/users/me
```

O campo `id` na resposta e o User ID.

#### Passo 6: Adicionar Variaveis de Ambiente

Adicione as seguintes linhas ao `.env` (ou variaveis no EasyPanel):

```env
# Conta MeLi: {NAME} -> empresa "{EMPRESA}" no dashboard
MELI_{NAME}_APP_ID={APP_ID}
MELI_{NAME}_SECRET_KEY={SECRET_KEY}
MELI_{NAME}_REFRESH_TOKEN={REFRESH_TOKEN_DO_PASSO_4}
MELI_{NAME}_USER_ID={USER_ID}
ACCOUNT_{NAME}_EMPRESA={NOME_NO_DASHBOARD}
```

**Exemplo concreto para uma nova conta "LOJA2":**
```env
# Conta MeLi: LOJA2 -> empresa "LOJA2 PECAS" no dashboard
MELI_LOJA2_APP_ID=9988776655443322
MELI_LOJA2_SECRET_KEY=AbCdEfGhIjKlMnOpQrStUvWx
MELI_LOJA2_REFRESH_TOKEN=TG-669a86724f1ed40001f050e8-555555555
MELI_LOJA2_USER_ID=555555555
ACCOUNT_LOJA2_EMPRESA=LOJA2 PECAS
```

#### Passo 7: Inserir Token no Supabase (opcional, mas recomendado)

O servico insere automaticamente na primeira execucao, mas para garantir:

**Via SQL Editor do Supabase:**
```sql
INSERT INTO meli_tokens (account_name, refresh_token, updated_at)
VALUES ('LOJA2', 'TG-669a86724f1ed40001f050e8-555555555', NOW())
ON CONFLICT (account_name)
DO UPDATE SET refresh_token = EXCLUDED.refresh_token, updated_at = NOW();
```

**Via API REST do Supabase:**
```bash
curl -X POST "https://iezxmhrjndzuckjcxihd.supabase.co/rest/v1/meli_tokens" \
  -H "apikey: {SUPABASE_SERVICE_KEY}" \
  -H "Authorization: Bearer {SUPABASE_SERVICE_KEY}" \
  -H "Content-Type: application/json" \
  -H "Prefer: resolution=merge-duplicates" \
  -d '{
    "account_name": "LOJA2",
    "refresh_token": "TG-669a86724f1ed40001f050e8-555555555",
    "updated_at": "2025-07-16T14:30:00-03:00"
  }'
```

#### Passo 8: Reiniciar o Servico

O servico descobre contas apenas no startup. Apos adicionar as env vars:

- **Docker/EasyPanel:** Reiniciar o container
- **Local:** Parar e reiniciar o uvicorn

Ao iniciar, o log deve mostrar:
```
Account loaded: LOJA2 -> empresa 'LOJA2 PECAS' (user 555555555)
```

#### Passo 9: Verificar

Apos reiniciar, confirme que a nova conta aparece:

```bash
curl http://localhost:8000/
```

Deve retornar `"accounts": ["141AIR", "LOJA2 PECAS", "NETAIR", "NETPARTS"]`

Force um sync manual para testar:
```bash
curl -X POST http://localhost:8000/sync
```

Verifique se o resultado da nova conta tem status `"synced"` ou `"no_sales"` (ambos sao sucesso).

---

### 11.2 Excluir Conta MeLi

#### Passo 1: Remover Variaveis de Ambiente

Remova (ou comente) TODAS as variaveis da conta do `.env`:

```env
# REMOVIDAS:
# MELI_LOJA2_APP_ID=...
# MELI_LOJA2_SECRET_KEY=...
# MELI_LOJA2_REFRESH_TOKEN=...
# MELI_LOJA2_USER_ID=...
# ACCOUNT_LOJA2_EMPRESA=...
```

#### Passo 2: Remover Token do Supabase

**Via SQL Editor:**
```sql
DELETE FROM meli_tokens WHERE account_name = 'LOJA2';
```

**Via API REST:**
```bash
curl -X DELETE "https://iezxmhrjndzuckjcxihd.supabase.co/rest/v1/meli_tokens?account_name=eq.LOJA2" \
  -H "apikey: {SUPABASE_SERVICE_KEY}" \
  -H "Authorization: Bearer {SUPABASE_SERVICE_KEY}"
```

#### Passo 3: Dados de Faturamento

**Os dados de faturamento existentes NAO sao deletados.** Os registros na tabela `faturamento` com `empresa = 'LOJA2 PECAS'` permanecem para historico.

Se voce QUISER remover os dados de faturamento (irreversivel):
```sql
DELETE FROM faturamento WHERE empresa = 'LOJA2 PECAS';
```

#### Passo 4: Reiniciar o Servico

Reiniciar o container/processo. A conta nao aparecera mais nos logs nem nos endpoints.

---

### 11.3 Atualizar Refresh Token Expirado

Quando o refresh token de uma conta expira (6 meses sem uso) ou e invalidado (crash no meio do fluxo), o sync retorna `status: "token_error"` para essa conta.

#### Passo 1: Gerar Novo Authorization Code

Abra no navegador (o dono da conta MeLi precisa estar logado):

```
https://auth.mercadolivre.com.br/authorization?response_type=code&client_id={APP_ID}&redirect_uri={REDIRECT_URI}
```

Para cada conta, use o APP_ID correspondente. Os APP_IDs estao no `.env`.

**Exemplos com as contas atuais:**

Para a conta **141** (APP_ID do .env):
```
https://auth.mercadolivre.com.br/authorization?response_type=code&client_id={MELI_141_APP_ID}&redirect_uri={REDIRECT_URI_CONFIGURADO}
```

Para a conta **NETAIR** (APP_ID do .env):
```
https://auth.mercadolivre.com.br/authorization?response_type=code&client_id={MELI_NETAIR_APP_ID}&redirect_uri={REDIRECT_URI_CONFIGURADO}
```

Copie o `code` da URL de redirecionamento.

#### Passo 2: Trocar Code por Token

```bash
curl -X POST https://api.mercadolibre.com/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id={APP_ID}" \
  -d "client_secret={SECRET_KEY}" \
  -d "code={CODE}" \
  -d "redirect_uri={REDIRECT_URI}"
```

Copie o `refresh_token` da resposta.

#### Passo 3: Atualizar no Supabase

**Via SQL Editor:**
```sql
UPDATE meli_tokens
SET refresh_token = 'TG-NOVO-TOKEN-AQUI',
    updated_at = NOW()
WHERE account_name = '141';
```

**Via API REST:**
```bash
curl -X PATCH "https://iezxmhrjndzuckjcxihd.supabase.co/rest/v1/meli_tokens?account_name=eq.141" \
  -H "apikey: {SUPABASE_SERVICE_KEY}" \
  -H "Authorization: Bearer {SUPABASE_SERVICE_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "TG-NOVO-TOKEN-AQUI", "updated_at": "2025-07-16T14:30:00-03:00"}'
```

#### Passo 4: Atualizar no .env (opcional mas recomendado)

Atualize tambem a env var `MELI_{NAME}_REFRESH_TOKEN` no `.env` para que, se o Supabase for resetado, o token seed esteja atualizado:

```env
MELI_141_REFRESH_TOKEN=TG-NOVO-TOKEN-AQUI
```

#### Passo 5: Reiniciar o Servico

Reiniciar para que o servico carregue o novo token do Supabase.

#### Passo 6: Verificar

```bash
curl -X POST http://localhost:8000/sync
```

O resultado da conta deve mostrar `status: "synced"` ou `status: "no_sales"` (nao mais `"token_error"`).

---

### 11.4 Checklist Rapido para LLMs

#### Para incluir nova conta, preciso:
- [ ] APP_ID e SECRET_KEY do app no MeLi Developer Portal
- [ ] Authorization code obtido via navegador (interativo, nao automatizavel)
- [ ] Trocar code por tokens via curl
- [ ] Adicionar 5 env vars: `MELI_{NAME}_APP_ID`, `MELI_{NAME}_SECRET_KEY`, `MELI_{NAME}_REFRESH_TOKEN`, `MELI_{NAME}_USER_ID`, `ACCOUNT_{NAME}_EMPRESA`
- [ ] (Opcional) Inserir token no Supabase via SQL/API
- [ ] Reiniciar o servico

#### Para excluir conta, preciso:
- [ ] Remover 5 env vars do .env
- [ ] Deletar registro de meli_tokens no Supabase
- [ ] Reiniciar o servico
- [ ] (Dados de faturamento ficam intactos)

#### Para corrigir token expirado, preciso:
- [ ] Novo authorization code via navegador (interativo)
- [ ] Trocar code por tokens via curl
- [ ] Atualizar refresh_token no Supabase (UPDATE ou PATCH)
- [ ] Atualizar env var (opcional)
- [ ] Reiniciar o servico

---

## 12. Deploy - Docker e EasyPanel

### Dockerfile:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Build e run local:

```bash
docker build -t meli-faturamento-sync .
docker run -p 8000:8000 --env-file .env meli-faturamento-sync
```

### EasyPanel:

O servico roda como container Docker no EasyPanel. As variaveis de ambiente sao configuradas na interface do EasyPanel (nao usa `.env` em producao).

---

## 13. Notas Tecnicas

### Timezone

Toda a logica de data usa **BRT (UTC-3)**. A variavel `BRT = timezone(timedelta(hours=-3))` e usada para:
- Determinar qual dia e "hoje" (para o filtro de pedidos)
- Formatar datas nos filtros da API MeLi
- Timestamps de atualizacao no Supabase

### Concorrencia

As contas sao processadas **sequencialmente** (nao em paralelo). Um unico `httpx.AsyncClient` e compartilhado por ciclo de sync. O timeout do client e 30 segundos.

### Limite de pedidos por dia

O servico tem um limite de seguranca de **500 pedidos por dia por conta** (10 paginas de 50). Se uma conta tiver mais de 500 pedidos/dia, os excedentes nao serao contabilizados. Para aumentar, alterar a condicao `offset > 500` em `_fetch_paid_orders_total()`.

### Valor usado: paid_amount vs total_amount

O servico usa `paid_amount` como valor principal. Esse campo inclui **produto + frete** (total pago pelo comprador). Se `paid_amount` for null ou 0, usa `total_amount` (so produto, sem frete) como fallback.

### Pedidos com fraude

Pedidos com a tag `"fraud_risk_detected"` sao ignorados e contabilizados separadamente no campo `fraud_skipped`.

### Persistencia de estado

O servico e **stateless** exceto por:
- `_last_sync` e `_last_results`: em memoria, perdidos no restart
- Tokens: persistidos no Supabase (sobrevivem restart)
- Faturamento: persistido no Supabase

### Seguranca

- O `.env` contem credenciais sensiveis e NAO deve ser versionado (esta no `.gitignore`)
- O SUPABASE_SERVICE_KEY tem acesso total (service_role) -- nao expor
- Os endpoints da API nao tem autenticacao (servico interno, nao exposto publicamente)
