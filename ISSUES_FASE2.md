# ISSUES FASE 2

Backlog da Fase 2 derivado do PRD-fase2 — cada issue é um **refinamento técnico** do PRD,
com parâmetros, interfaces, schemas e invariantes suficientes para implementar sem consultar
o PRD. Organizadas em vertical slices tracer-bullet, ordem de dependência.

---

## 1) Contrato v1 de Sessão e OpenAPI Base
Tipo: AFK

### Context
O componente precisa expor uma API HTTP versionada `/v1` para que o app de telemedicina
gerencie o ciclo de vida de Sessão. O `session_kind` (perfil) é passado por chamada — o
componente não decide perfil no boot. Toda tabela tem `tenant_id NOT NULL DEFAULT
'00000000-0000-0000-0000-000000000001'` (UUID default da Fase 2); `SessionContext` circula
por todos os resolvers desde o dia 1.

### What to build

**Endpoints obrigatórios (Fase 2.0):**

| Método + Path | Authn | Descrição |
|---|---|---|
| `POST /v1/sessions` | JWT Bearer | Criar Sessão, retornar `{session_id, ws_url, ws_token}` |
| `POST /v1/sessions/{id}/close` | JWT Bearer | Encerrar Sessão, iniciar materialização do draft |
| `GET /v1/sessions/{id}/draft` | JWT Bearer | Ler draft materializado do Postgres |
| `POST /v1/sessions/{id}/token/refresh` | JWT Bearer | Renovar ws_token sem fechar WS |
| `POST /v1/sessions/{id}/draft/retry` | JWT Bearer | Retry de materialização em status `failed` |
| `POST /v1/sessions/{id}/draft/feedback` | JWT Bearer | Ingerir feedback de correção manual do app |

**Request `POST /v1/sessions`:**
```json
{
  "session_kind": "medical",
  "idempotency_key": "<string>",
  "actor_role": "doctor|patient",
  "seniority": null,
  "plan": "basico|medio|premium"
}
```

**Response `POST /v1/sessions` (201):**
```json
{
  "session_id": "<uuid>",
  "ws_url": "wss://host/v1/sessions/{id}/audio",
  "ws_token": "<uuid-v4>",
  "session_kind": "medical",
  "plan_features": {
    "stt_mode": "streaming|batch",
    "llm_realtime_red_flag": true,
    "llm_proactive_questions": true,
    "llm_prontuario_model": "gpt-4o|gpt-4o-mini",
    "session_max_minutes": 60
  }
}
```

**`SessionContext` obrigatório em todos os resolvers:**
```python
@dataclass
class SessionContext:
    tenant_id: str   # UUID default na Fase 2
    session_id: str
    session_kind: str
    seniority: str | None
    actor_role: str
    plan: str
```

**Hard caps de Sessão:**
- Duração máxima: 4h (hard cap)
- Warning a 30 min de silêncio (`{"type":"status","reason":"silence_warning"}`)
- Auto-finalize: 6h após criação (independente de silêncio)
- `plan_features` imutáveis após criação da Sessão — mudança de plano vale na próxima criação

**Draft — estados:**
- `pending` → `GET /draft` retorna `202` com `retry-after: 5`
- `ready` → `GET /draft` retorna `200` com conteúdo completo
- `failed` → `GET /draft` retorna `500` com `{"error":"DRAFT_MATERIALIZATION_FAILED","partial":true,...}`

**OpenAPI:**
- Schema gerado em `schemas/openapi/v1.yaml` via FastAPI `get_openapi()`
- Versão da API no header `X-API-Version: 1.0`
- Stable error codes novos com namespace `<DOMAIN>_<COMPONENT>_<REASON>`; códigos legados
  (`STT_EMPTY`, `LLM_ERR_UNAVAILABLE` etc.) preservados sem rename

### Acceptance criteria
- [ ] `POST /v1/sessions` com `session_kind=medical` e `plan=premium` retorna `201` com campos
  `session_id`, `ws_url`, `ws_token`, `plan_features` preenchidos
- [ ] `SessionContext` com `tenant_id` default é injetado em todos os resolvers (validado por
  assert nos testes de integração)
- [ ] `GET /draft` retorna `202` quando `draft_status=pending` e `200` quando `ready`
- [ ] `GET /draft` retorna `500` com `partial:true` quando `failed`
- [ ] `POST /v1/sessions/{id}/close` inicia materialização (muda `draft_status` para `pending`)
- [ ] `plan_features` reflete corretamente os toggles do plano (`stt_mode=batch` para Básico,
  `streaming` para Médio/Premium)
- [ ] Hard caps registrados na Sessão: `auto_finalize_at = created_at + 6h`
- [ ] OpenAPI `v1.yaml` gerado e válido (sem erros de schema)
- [ ] Todos os endpoints protegidos rejeitam request sem `Authorization: Bearer` com `401`
- [ ] Novos error codes seguem namespace `<DOMAIN>_<COMPONENT>_<REASON>`

### Blocked by
None — pode iniciar imediatamente

---

## 2) Idempotency-Key Escopada e Semântica de Replay
Tipo: AFK

### Context
`POST /v1/sessions` deve ser seguro de reenviar em retry de rede. A garantia é
first-write-wins com escopo canônico por `(tenant_id, endpoint, actor_sub_jwt,
idempotency_key)` — chave igual em endpoint/ator diferente não colide. TTL 24h.

### What to build

**Escopo canônico (uniqueness constraint):**
```
UNIQUE (tenant_id, endpoint, actor_sub_jwt, idempotency_key)
```
- `endpoint`: string canônica `POST /v1/sessions`
- `actor_sub_jwt`: claim `sub` do JWT do chamador
- `idempotency_key`: valor passado pelo app via header `Idempotency-Key`
- `tenant_id`: do `SessionContext`

**Tabela `idempotency_keys`:**
```sql
CREATE TABLE idempotency_keys (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL DEFAULT 'default-uuid',
    endpoint       TEXT NOT NULL,
    actor_sub_jwt  TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response_body  JSONB NOT NULL,
    response_status INT NOT NULL,
    payload_hash   TEXT NOT NULL,  -- SHA-256 do request body canônico
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, endpoint, actor_sub_jwt, idempotency_key)
);
```

**Comportamento:**
1. Sem `Idempotency-Key` no header → `POST /v1/sessions` retorna `400
   IDEMPOTENCY_KEY_MISSING`
2. Primeira chamada: `INSERT ... ON CONFLICT DO NOTHING` — se inserido, processa e grava
   `response_body + payload_hash`
3. Chamada duplicada dentro do TTL:
   - Mesmo `payload_hash`: retorna `response_body` original com header `Idempotency-Replayed:
     true`
   - `payload_hash` diferente: retorna `409 IDEMPOTENCY_PAYLOAD_MISMATCH`
4. Após TTL: chave expirada tratada como nova (nova Sessão criada normalmente)

**TTL:** 24h (`expires_at = created_at + INTERVAL '24 hours'`)

**payload_hash:** SHA-256 de `json.dumps(body, sort_keys=True, ensure_ascii=False)` encode
UTF-8 — canônico e determinístico

**Limpeza:** job Postgres `DELETE FROM idempotency_keys WHERE expires_at < now()` agendado
diariamente (ou via pg_cron)

### Acceptance criteria
- [ ] Request sem `Idempotency-Key` retorna `400 IDEMPOTENCY_KEY_MISSING`
- [ ] Segundo POST idêntico (mesmo payload) dentro de 24h retorna `201` com body original e
  header `Idempotency-Replayed: true`
- [ ] Segundo POST com mesma chave mas payload diferente retorna `409
  IDEMPOTENCY_PAYLOAD_MISMATCH`
- [ ] Chave de tenant A não colide com chave idêntica de tenant B (uniqueness por tenant_id)
- [ ] Chave de `actor_sub=doctor1` não colide com chave idêntica de `actor_sub=doctor2`
- [ ] `payload_hash` é SHA-256 canônico do body (testado com body com chaves fora de ordem)
- [ ] Registro expirado (> 24h) resulta em nova Sessão criada normalmente
- [ ] Migration Alembic com `downgrade()` obrigatório

### Blocked by
- #1

---

## 3) Ingestão WS de Áudio com Reconnect, Dedupe e Drain Gracioso
Tipo: AFK

### Context
`AudioGateway` termina o WebSocket bidirecional (Opus → PCM) e alimenta o pipeline STT.
Dois requisitos críticos: (a) reconexão sem duplicação/perda de eventos finais de transcrição;
(b) drain gracioso no shutdown de container ECS sem perder estado aberto de Sessão.

### What to build

**Codec:** Opus 20ms, 16kHz, mono. Cada frame WS carrega `{"seq":<N>,...}` (monotônico
por stream, por Sessão).

**Diarização por roteamento:** duas streams WS separadas por Sessão (Médico e Paciente).
Sticky routing via ALB com header `X-Session-Id`; ambas as streams de uma Sessão caem no
mesmo container ECS.

**Bounded queue AudioGateway:** 1s de buffer por Sessão (≈ 50 frames Opus de 20ms).
Overflow → emite `{"type":"status","reason":"STT_OVERFLOW"}` via WS, dropa frames,
mantém sessão viva (não fecha WS).

**Handshake de reconexão:**
1. Cliente reconecta com `ws_token` e header `X-Session-Id`
2. Componente responde:
   ```json
   {"type":"session_resumed","last_event_id":"<redis-stream-id>","last_seq":42}
   ```
3. Cliente reenvia apenas frames com `seq > last_seq`
4. Sem ACK/NACK por frame — garantia at-least-once com dedupe server-side

**Dedupe server-side:** por `event_id` no Redis Streams — cada evento de transcrição final
carrega `event_id` único; duplicatas silenciosamente descartadas no `XADD` (ID explícito
com `XADD ... NX`).

**Redis Stream:** `XADD session:{tenant_id}:{session_id}:events` com campo
`server_timestamp_ms` adicionado pelo AudioGateway — relógio de referência para ordenação
cronológica do diálogo.

**Janela de reordenação cross-stream:** 200ms. Eventos de streams diferentes (Médico vs.
Paciente) chegados dentro de 200ms são ordenados por `server_timestamp_ms` antes de commitar
ao `ConversationState`.

**Late arrival:** eventos com `server_timestamp_ms < watermark_atual − 200ms` são commitados
imediatamente com:
```json
{"metadata": {"late_arrival": true, "expected_position": "<event_id_do_gap>"}}
```
Nunca descartados. Métrica `events_late_arrival_ratio` exposta — alerta SRE se > 1% em
janela de 5min.

**Drain gracioso (ECS shutdown):**
1. Recebe SIGTERM → emite `{"type":"drain","reconnect_in":30}` via WS em todas as Sessões
   ativas
2. Rejeita novas Sessões com `503 SERVICE_DRAINING`
3. Aguarda até 90s para LLM calls em voo terminarem
4. Após 90s: cancela LLM calls restantes, commita resultado parcial como evento
   `partial_complete` no Redis Stream — `ConversationState` nunca fica em estado aberto sem
   evento de fechamento
5. `ECS_CONTAINER_STOP_TIMEOUT` = 120s (configurado no task definition, default ECS é 30s —
   sem isso o drain de 90s é truncado)

**Snapshot de estado (EventStream):**
- A cada 100 eventos ou 5min (o que ocorrer primeiro): serializar estado em
  `session:{tenant_id}:{session_id}:state_snapshot` com TTL = `session_max_minutes`
- Perfil `medical`: snapshot a cada 25 eventos ou 1min (4× mais frequente)
- Na reconexão pós-crash: carregar snapshot + reduce do delta (≤ 100 eventos)
- Redis Stream expira 24h após close

### Acceptance criteria
- [ ] Frame Opus 20ms recebido via WS é decodificado para PCM 16kHz mono sem erro
- [ ] `seq` monotônico por stream — frame fora de ordem é aceito e marcado `late_arrival`
- [ ] Reconexão dentro de 5s: handshake retorna `last_seq` correto e cliente não recebe
  duplicatas de eventos finais já emitidos
- [ ] Overflow de queue (> 1s buffer) emite `STT_OVERFLOW` e mantém WS aberta
- [ ] Ambas as streams WS (Médico + Paciente) de uma Sessão chegam ao mesmo container (sticky
  routing validado em teste de integração com mock ALB)
- [ ] Late arrival `server_timestamp_ms < watermark − 200ms` → `late_arrival:true` no evento
- [ ] `events_late_arrival_ratio` exportado como métrica Prometheus
- [ ] Drain: SIGTERM → `{"type":"drain","reconnect_in":30}` emitido em < 1s; container não
  aceita nova Sessão após SIGTERM; `ECS_CONTAINER_STOP_TIMEOUT=120` no task definition
- [ ] Snapshot Postgres/Redis atualizado a cada 25 eventos para perfil `medical` (validado com
  mock de Redis)
- [ ] `ConversationState` após reconexão pós-crash (snapshot + delta) equivale ao estado
  completo (teste de equivalência com 100 eventos sintéticos)
- [ ] Migration Alembic com `downgrade()` obrigatório para qualquer DDL novo

### Blocked by
- #1
- #2

---

## 4) STT Medical Completude-First com STT_CONFIDENCE e Fallback Batch
Tipo: AFK

### Context
STT primário é Deepgram Nova-2 streaming pt-BR. Fallback batch é OpenAI Whisper API.
Para perfil `medical`, o princípio é completude/acurácia — transcrição final por bloco,
parcial ao vivo opcional e desligada por padrão. `STT_CONFIDENCE` é o único limiar
operacional de aceitação.

### What to build

**Contrato `STTService.stream`:**
```python
async def stream(
    audio_chunks_iter: AsyncIterator[bytes],
    on_partial: Callable[[STTEvent], None] | None,
    on_final: Callable[[STTEvent], None],
    *,
    sample_rate: int = 16000,
) -> AsyncIterator[STTEvent]:
    ...

@dataclass
class STTEvent:
    text: str
    confidence: float        # 0.0 – 1.0
    is_final: bool
    event_id: str            # UUID v4 para dedupe
    speaker: str | None      # "doctor" | "patient" | None
    uncertain: bool          # True se confidence < STT_CONFIDENCE
```

**Configuração `STT_CONFIDENCE`:**
- Env var `STT_CONFIDENCE`, range `[0.30, 0.95]`
- Ausente ou fora do range → warning log + fallback para `0.55` (não interrompe Sessão)
- Semântica: limiar único de aceitação — abaixo → segmento marcado como `uncertain=True`
  no evento e prefixado com `[incerto]` no draft

**Timeout Deepgram por segmento:** 800ms (fixo na 2.0; política adaptativa na 2.1).
Estouro → frame re-enfileirado para fallback Whisper API daquele segmento.

**Circuit breaker STT (Deepgram → Whisper batch):**
- **Abrir:** N=3 falhas consecutivas de Deepgram (timeout OU erro de conexão)
- **Estado open:** componente usa exclusivamente Whisper API batch; emite via WS:
  `{"type":"status","stt_mode":"fallback_batch"}`
- **Janela mínima no fallback:** 60s (não tenta retornar ao streaming antes disso)
- **Critério de retorno:** 5 health checks de Deepgram consecutivos com sucesso (probe de
  conexão leve, sem áudio real) → fecha circuit breaker → emite:
  `{"type":"status","stt_mode":"streaming"}`
- **Sessão nunca interrompida durante fallback** — SLO degradado: draft em até 5min após
  encerramento (mesmo contrato do Plano Básico normal)

**Perfil `medical` — completude-first:**
- Transcrição final por bloco (default) — sem emissão de partials via WS
- `MEDICAL_STT_PARTIAL_ENABLED=0` por padrão; `=1` habilita partials ao vivo (opt-in)
- Trechos com `uncertain=True` são marcados como `[incerto]` no rascunho do draft e
  sinalizados para revisão médica

**`STTService.transcribe(...)` legacy:** permanece coexistindo; Voice CLI offline não
afetada.

**Qualidade — bateria de validação:**
- WER ≤ 10% em 50 frases clínicas pt-BR fixas com Deepgram Nova-2 (antes de fechar 2.0)
- Bateria RED_FLAG no web (Deepgram) — falha bloqueia go-live

### Acceptance criteria
- [ ] `STT_CONFIDENCE=0.45` → segmentos com confidence < 0.45 recebem `uncertain=True`
- [ ] `STT_CONFIDENCE` ausente → warning no log + efetivo `0.55`
- [ ] `STT_CONFIDENCE=0.20` (abaixo de 0.30) → warning + fallback 0.55
- [ ] Timeout Deepgram > 800ms → frame re-enfileirado para Whisper API (mock de timeout no
  teste)
- [ ] 3 falhas consecutivas Deepgram → circuit breaker abre + evento WS
  `stt_mode:fallback_batch` emitido
- [ ] Whisper batch processa o áudio durante fallback sem perder Sessão
- [ ] 5 health checks Deepgram OK após ≥ 60s → circuit breaker fecha + evento WS
  `stt_mode:streaming` emitido
- [ ] Perfil `medical` com `MEDICAL_STT_PARTIAL_ENABLED=0` (default): nenhum evento `partial`
  emitido via WS
- [ ] Segmento `uncertain=True` → draft contém prefixo `[incerto]` para o campo
- [ ] WER ≤ 10% na bateria de 50 frases clínicas (teste de aceitação pré-go-live)
- [ ] `STTService.transcribe(...)` legacy não quebra (testes existentes do MVP passam)

### Gap tests (Issue #3 — implementar junto com esta issue)
- [ ] GAP-TTL1 — `FakeEventStream.expire()` registra TTL correto: após `close`, stream key tem TTL = `STREAM_TTL_SECONDS` (86400s)
- [ ] GAP-SAD1 — cliente WS desconecta abruptamente durante `process_frame`: nenhuma exceção não tratada propaga; `unregister_drain_callback()` é chamado
- [ ] GAP-WS1 — fluxo completo via `starlette.testclient` WebSocket: connect → enviar frame → receber `{"type":"status","reason":"STT_OVERFLOW"}` ao estourar fila (sem browser)
- [ ] GAP-WS2 — reconexão testada pelo router: client envia `{"type":"reconnect"}` via TestClient WS e recebe handshake `{"type":"session_resumed","last_seq":N}`

### Blocked by
- #3

---

## 5) Pipeline de Draft no Postgres com Materialização Idempotente
Tipo: AFK

### Context
Ao encerrar a Sessão (`close` ou `auto-finalize`), o draft é materializado atomicamente
no Postgres. O `GET /draft` lê sempre do Postgres — nunca do Redis Stream diretamente.
A materialização deve ser idempotente sob corridas (close concorrente com auto-finalize,
retry simultâneo).

### What to build

**Schema `drafts`:**
```sql
CREATE TABLE drafts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL DEFAULT 'default-uuid',
    session_id          UUID NOT NULL REFERENCES sessions(id),
    draft_status        TEXT NOT NULL CHECK (draft_status IN ('pending','ready','failed')),
    content             JSONB,
    partial             BOOLEAN NOT NULL DEFAULT false,
    retry_count         INT NOT NULL DEFAULT 0,
    last_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, session_id)
);
```

**Materialização idempotente — mecanismo:**
```sql
SELECT pg_advisory_xact_lock(hashtext('materialize:' || session_id::text));
-- check-and-set em draft_status
UPDATE drafts SET draft_status = 'ready', content = :content
WHERE session_id = :sid AND draft_status = 'pending';
```
- Chave canônica do lock: `materialize:{tenant_id}:{session_id}`
- Corrida close + auto-finalize: o segundo worker pega o lock, vê `ready`, sai silenciosamente
- Retry simultâneo: idem

**Retry automático:** 3× com backoff exponencial (1s, 2s, 4s) antes de marcar `failed`.
Após `failed`: alerta SRE + `POST /v1/sessions/{id}/draft/retry` reprocessa usando Redis
Stream (≤24h) ou AuditLog como fallback.

**`GET /v1/sessions/{id}/draft`:**
- `pending` → `202 Accepted` com `retry-after: 5`
- `ready` → `200 OK` com conteúdo completo
- `failed` → `500` com `{"error":"DRAFT_MATERIALIZATION_FAILED","partial":true,"content":<parcial>}`

**`POST /v1/sessions/{id}/draft/feedback`:**
```json
{
  "chars_original": 1200,
  "chars_edited": 180,
  "fields_corrected": ["queixa_principal","historico_familiar"]
}
```
Armazenado em `draft_feedback` para telemetria de qualidade clínica (Issue #9).

**Snapshot periódico (EventStream → ConversationState):**
- Perfis não-`medical`: snapshot a cada 100 eventos ou 5min
- Perfil `medical`: snapshot a cada 25 eventos ou 1min (4× mais frequente)
- Perda máxima de contexto LLM num crash abrupto: 100 eventos (não-medical) / 25 eventos
  (medical)
- **Critério de aceitabilidade clínica:** bateria automatizada que reproduz crash em N eventos
  e compara draft resultante contra ground truth — perda > 5% de campos obrigatórios da
  Anamnese bloqueia release (owner: Med Lead + DPO, revisão trimestral)

**Stream Redis:** expira 24h após close (`EXPIRE session:{tid}:{sid}:events 86400`).

### Acceptance criteria
- [ ] `POST /v1/sessions/{id}/close` → `drafts` com `draft_status=pending` inserido
- [ ] Worker de materialização transita `pending → ready` com `content` preenchido
- [ ] Lock advisory garante exactly-once: close + auto-finalize simultâneos resultam em exatamente
  um `ready` (teste de concorrência com 2 threads)
- [ ] Retry simultâneo de materialização: segundo worker sai silenciosamente sem `DUPLICATE_KEY`
- [ ] 3 falhas de materialização → `draft_status=failed`, alerta (log nível ERROR)
- [ ] `GET /draft` retorna `202` com `retry-after: 5` quando `pending`
- [ ] `GET /draft` retorna `500` com `partial:true` quando `failed`
- [ ] `POST /draft/feedback` armazena `chars_original`, `chars_edited`, `fields_corrected`
- [ ] Snapshot `medical` atualizado a cada 25 eventos (validado com mock de 30 eventos)
- [ ] Bateria de crash em N=25 eventos para `medical`: draft resultante perde ≤ 5% de campos
  obrigatórios da Anamnese
- [ ] Redis Stream com `EXPIRE 86400` após `close` (validado com mock Redis TTL check)
- [ ] Migration Alembic com `downgrade()` obrigatório

### Gap tests (Issue #3 — implementar junto com esta issue)
- [ ] GAP-CONC1 — dois `AudioGateway` para a mesma sessão processando frames simultaneamente (2 threads asyncio): stream Redis não corrompido, nenhum `event_id` duplicado

### Blocked by
- #3
- #4

---

## 6) Auth End-to-End: JWT do App + ws_token + Revogação Imediata
Tipo: AFK

### Context
Dois tokens com ciclos de vida distintos: (a) JWT Bearer para endpoints REST; (b)
ws_token opaco (UUID v4) exclusivo para a conexão WebSocket. Fechamento/revogação derruba
o WS ativo em < 2s end-to-end sem esperar próximo frame.

### What to build

**JWT do app (endpoints REST `POST /v1/sessions` e `POST /v1/sessions/{id}/token/refresh`):**

| Regra | Valor |
|---|---|
| Algoritmos aceitos | RS256 (preferencial), ES256 |
| Algoritmos rejeitados | HS256, `none` → `401 AUTH_ALGORITHM_NOT_ALLOWED` |
| Claims obrigatórios | `iss`, `aud`, `sub`, `exp`, `nbf`, `iat`, `jti` |
| `iss` | URL do IdP do app, validada contra allow-list em env `JWT_ISS_ALLOWLIST` |
| `aud` | `urn:assistente-voz:fase2` (valor fixo) |
| Janela máxima `exp − iat` | 900s (15 min) → maior → `401 AUTH_JWT_WINDOW_EXCEEDED` |
| Clock skew tolerado | ±60s |
| Anti-replay `jti` | Cache LRU de `jti` por 15 min, dimensionado para pico de criação de Sessão |

**JWKS:**
- Cache TTL 10min com refresh on-miss
- `kid` desconhecido → refresh imediato (um único retry)
- Falha de refresh: mantém cache anterior por até 60min
- Após 60min sem JWKS válido: degrada para `503 AUTH_JWKS_UNAVAILABLE` no endpoint
  protegido

**ws_token:**
- UUID v4 gerado na criação da Sessão
- Armazenado no Redis: `ws:token:{ws_token}` → `{session_id, tenant_id, expires_at}`
- TTL = `session_max_minutes` do plano (60min Básico/Médio, 240min Premium)
- Validado **apenas** no estabelecimento da conexão WS e na renovação — **não por frame**

**Renovação (`POST /v1/sessions/{id}/token/refresh`):**
- Autenticado com JWT do app
- Retorna novo `ws_token`, invalida o anterior atomicamente no Redis (pipeline Redis:
  DEL antigo + SET novo)
- WS existente **não é fechado** — browser recebe novo token via canal próprio do app
- Componente emite `{"type":"token_expiring_in":300}` via WS com 5min de antecedência

**Revogação ativa (`POST /v1/sessions/{id}/close`):**
1. Apaga `ws:token:{ws_token}` do Redis (< 1s)
2. Publica `revoke:{ws_token}` em canal Redis pub/sub `ws:revocations`
3. Todos os containers assinam `ws:revocations`; o container que detém a conexão fecha o WS
   com close code `4002 session_revoked` em < 2s end-to-end
4. Mesmo mecanismo para revogação fora-de-banda (resposta a incidente)

**Close codes WS:**
- `4001`: `auth_invalid` (ws_token inválido/expirado no handshake)
- `4002`: `session_revoked` (revogação ativa por close/incidente)

**Respostas de erro:**
- JWT inválido/expirado → `401`
- ws_token inválido → fechar WS com `4001`
- ws_token inválido por revogação ativa → fechar WS com `4002`

### Acceptance criteria
- [ ] JWT com RS256 válido, claims obrigatórios presentes → request aceito
- [ ] JWT com HS256 → `401 AUTH_ALGORITHM_NOT_ALLOWED`
- [ ] JWT sem `jti` → `401` (claim obrigatório ausente)
- [ ] `iss` não na allowlist → `401`
- [ ] `exp − iat > 900s` → `401 AUTH_JWT_WINDOW_EXCEEDED`
- [ ] Clock skew de 59s (exp no futuro por 59s) → aceito; 61s além do skew → rejeitado
- [ ] `jti` reutilizado dentro de 15min → `401` (anti-replay)
- [ ] `kid` desconhecido → JWKS refreshed imediatamente; JWT validado com nova chave
- [ ] JWKS indisponível por > 60min → endpoint retorna `503 AUTH_JWKS_UNAVAILABLE`
- [ ] ws_token UUID v4 gerado e armazenado no Redis com TTL correto por plano
- [ ] WS estabelecido com ws_token válido → conexão aceita
- [ ] WS com ws_token expirado → close `4001`
- [ ] `POST /close` → ws_token apagado do Redis + pub/sub `revoke:{token}` publicado
- [ ] WS fechado com `4002` em < 2s após `POST /close` (medido com mock Redis pub/sub)
- [ ] `POST /token/refresh` invalida token antigo atomicamente e não fecha WS ativo
- [ ] `{"type":"token_expiring_in":300}` emitido via WS com 5min de antecedência

### Gap tests (Issue #3 — implementar junto com esta issue)
- [ ] GAP-SAD2 — banco cai durante validação de `ws_token` (mock de `AsyncSession` lançando exceção): WS fecha com código 4001 ou 1011, sem stack trace exposto ao cliente

### Blocked by
- #1
- #3

---

## 7) Fallback STT com Circuit Breaker, Status de Degradação e Recuperação
Tipo: AFK

### Context
Refinamento do circuit breaker introduzido na #4 — aqui entregamos a implementação
completa com parâmetros fixos para a 2.0, testes determinísticos de abertura/fechamento
e visibilidade operacional para o SRE.

### What to build

**Parâmetros do circuit breaker (fixos na 2.0):**
```python
@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3        # N falhas consecutivas para abrir
    fallback_min_window_s: int = 60   # janela mínima em fallback antes de sondar
    health_check_success_required: int = 5  # probes OK consecutivos para fechar
    probe_interval_s: int = 10        # intervalo entre probes de health check
    timeout_per_segment_ms: int = 800 # timeout Deepgram por segmento
```

**Estados do circuit breaker:**
```
CLOSED → (3 falhas consecutivas) → OPEN → (60s + 5 probes OK) → CLOSED
                                      ↑
              (falha durante probe) → permanece OPEN, reinicia contagem de probes
```

**Probe de health check:** conexão TCP + WebSocket handshake com Deepgram (sem enviar
áudio real). Falha na probe reinicia a contagem dos 5 probes — não reabre o timer de 60s.

**Eventos WS emitidos:**
```json
{"type":"status","stt_mode":"fallback_batch","reason":"circuit_open"}
{"type":"status","stt_mode":"streaming","reason":"circuit_closed"}
{"type":"status","stt_mode":"fallback_batch","reason":"timeout"}
```

**SLO degradado durante fallback:** draft disponível em até 5min após encerramento da
Sessão (mesmo SLO do Plano Básico normal). Sessão não é encerrada automaticamente.

**Métricas Prometheus:**
```
stt_circuit_state{stt_mode="deepgram|whisper"}  # 0=closed, 1=open
stt_fallback_total{reason="timeout|connection|consecutive_failures"}
stt_segment_duration_ms (histogram)
```

**Stub de integração 2.0:** Deepgram client mockável via interface `DeepgramBackend`
— troca por mock em testes sem alterar lógica do circuit breaker.

### Acceptance criteria
- [ ] 3 timeouts Deepgram consecutivos (mock 801ms) → circuit abre; evento WS
  `stt_mode:fallback_batch` emitido em < 1s
- [ ] Circuit aberto → Whisper API batch processa segmentos sem erro
- [ ] Circuit aberto há < 60s → probe não iniciada (clock mockado no teste)
- [ ] Após 60s: 4 probes OK → circuit permanece open; 5ª probe OK → circuit fecha; evento WS
  `stt_mode:streaming` emitido
- [ ] Falha em qualquer uma das 5 probes → contagem reinicia; 5s probes OK subsequentes
  necessários
- [ ] Sessão permanece ativa (sem close/WS drop) durante toda a transição de fallback
- [ ] `stt_circuit_state` exportado como métrica Prometheus (validado com mock Prometheus
  registry)
- [ ] `CircuitBreakerConfig` carregado de env vars com defaults corretos

### Blocked by
- #4
- #6

---

## 8) Confiabilidade por Campo no Draft (Feature Flag via Env)
Tipo: AFK

### Context
O draft pode classificar cada campo extraído com um indicador de confiança baseado no
`confidence` dos segmentos STT que contribuíram para aquele campo. Habilitado por env
para não impactar o caminho crítico quando desligado.

### What to build

**Env vars:**
```
DRAFT_FIELD_CONFIDENCE_ENABLED=0        # 0 = desligado (default), 1 = ligado
FIELD_CONFIDENCE_REVIEW_THRESHOLD=0.55  # abaixo → classificação "revisar"
FIELD_CONFIDENCE_HIGH_THRESHOLD=0.80    # igual ou acima → classificação "alto"
```
Validação: `REVIEW_THRESHOLD < HIGH_THRESHOLD`; fora do range `[0.30, 0.95]` → warning
+ fallback para os defaults acima.

**Classificação por campo:**
```python
def classify_field_confidence(field_confidence: float) -> str:
    if field_confidence < FIELD_CONFIDENCE_REVIEW_THRESHOLD:
        return "revisar"
    elif field_confidence < FIELD_CONFIDENCE_HIGH_THRESHOLD:
        return "medio"
    else:
        return "alto"
```
`field_confidence` = média ponderada do `confidence` dos segmentos STT que contribuíram
para o campo.

**Schema do draft com confiança (quando `DRAFT_FIELD_CONFIDENCE_ENABLED=1`):**
```json
{
  "fields": {
    "queixa_principal": {
      "value": "dor de cabeça intensa há 3 dias",
      "confidence_class": "revisar",
      "field_confidence": 0.48,
      "uncertain_segments": ["[incerto] há 3 dias"]
    },
    "historico_familiar": {
      "value": "hipertensão paterna",
      "confidence_class": "alto",
      "field_confidence": 0.87,
      "uncertain_segments": []
    }
  }
}
```

**Quando `DRAFT_FIELD_CONFIDENCE_ENABLED=0`:** campos não contêm `confidence_class` nem
`field_confidence` — schema de resposta idêntico ao sem feature flag.

**Marcação de incerteza no draft:** segmentos com `uncertain=True` (confidence <
`STT_CONFIDENCE`) são listados em `uncertain_segments` e prefixados com `[incerto]` no
`value` do campo.

### Acceptance criteria
- [ ] `DRAFT_FIELD_CONFIDENCE_ENABLED=0` → draft não contém `confidence_class` nem
  `field_confidence` em nenhum campo
- [ ] `DRAFT_FIELD_CONFIDENCE_ENABLED=1` → todos os campos do draft contêm
  `confidence_class` com valor em `{revisar, medio, alto}`
- [ ] Campo com `field_confidence=0.42` → `confidence_class=revisar`
- [ ] Campo com `field_confidence=0.67` → `confidence_class=medio`
- [ ] Campo com `field_confidence=0.85` → `confidence_class=alto`
- [ ] `FIELD_CONFIDENCE_REVIEW_THRESHOLD=0.70` env → threshold respeitado no runtime
- [ ] `FIELD_CONFIDENCE_REVIEW_THRESHOLD > FIELD_CONFIDENCE_HIGH_THRESHOLD` → warning +
  fallback para defaults
- [ ] Segmento `uncertain=True` → aparece em `uncertain_segments` e prefixado `[incerto]`
  no `value`
- [ ] Feature flag pode ser alterada entre Sessões sem restart (relida no início de cada
  materialização de draft)

### Blocked by
- #5

---

## 9) Telemetria de Qualidade Clínica e Feedback de Correção
Tipo: AFK

### Context
A qualidade operacional do draft é medida por duas métricas semânticas (complementares
ao WER): taxa de correção manual e taxa de contradição factual. O kill switch garante que
transcrição nunca para mesmo quando a geração de draft é desabilitada.

### What to build

**Métricas de qualidade clínica (Fase 2.1 SLO):**
- **Taxa de correção manual:** `chars_edited / chars_original` por Sessão `medical`
  - SLO: ≤ 30% por Sessão
  - Kill switch: > 50% em 2 Sessões consecutivas do mesmo Médico → desabilita geração de
    draft (transcrição segue ativa); alerta SRE
- **Taxa de contradição factual:** ratio de afirmações do draft contraditórias à
  transcrição (validador automático)
  - SLO: ≤ 2% agregado semanal
  - Kill switch: > 10% de contradição factual agregada na semana → kill switch draft

**`POST /v1/sessions/{id}/draft/feedback` — payload:**
```json
{
  "chars_original": 1200,
  "chars_edited": 180,
  "correction_ratio": 0.15,
  "fields_corrected": ["queixa_principal", "historico_familiar"],
  "session_kind": "medical",
  "actor_sub": "<sub do JWT>"
}
```

**Tabela `draft_feedback`:**
```sql
CREATE TABLE draft_feedback (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL DEFAULT 'default-uuid',
    session_id      UUID NOT NULL REFERENCES sessions(id),
    chars_original  INT NOT NULL,
    chars_edited    INT NOT NULL,
    correction_ratio FLOAT NOT NULL,
    fields_corrected TEXT[] NOT NULL,
    session_kind    TEXT NOT NULL,
    actor_sub       TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Métricas Prometheus/OTel:**
```
draft_correction_ratio{session_kind, actor_sub}   # histogram
draft_factual_contradiction_ratio{session_kind}    # gauge semanal
draft_kill_switch_active{reason}                   # 0|1
events_late_arrival_ratio                          # da Issue #3
stt_circuit_state                                  # da Issue #7
```

**Observabilidade de infraestrutura (obrigatória na 2.0):**
- OpenTelemetry com traces W3C distribuídos: navegador → AudioGateway → SttPool → LLM
- structlog JSON com campos: `correlation_id`, `session_id`, `tenant_id`, `actor_role`
- Loki via Vector para agregação de logs
- Prometheus com label `tenant_id="default"` desde o dia 1
- Alerta de uso de provider a 70% do teto (Deepgram concurrent, OpenAI tokens/min)

**Kill switch de draft:**
- Feature flag `DRAFT_GENERATION_ENABLED=1` (default) — pode ser `=0` para desabilitar
  geração de draft globalmente (transcrição nunca para)
- Kill switch automático (implementado como set de `DRAFT_GENERATION_ENABLED=0` em
  Redis shared config) ativado pelos critérios acima; SRE notificado via PagerDuty

### Acceptance criteria
- [ ] `POST /v1/sessions/{id}/draft/feedback` armazena campos em `draft_feedback` com
  `tenant_id` correto
- [ ] `correction_ratio` calculado como `chars_edited / chars_original` (validado com
  dados sintéticos)
- [ ] Métrica `draft_correction_ratio` exportada como histogram no Prometheus
- [ ] Kill switch automático ativado quando `correction_ratio > 0.50` em 2 Sessões
  consecutivas do mesmo `actor_sub` (testado com mock de 2 feedbacks)
- [ ] Transcrição continua ativa quando kill switch de draft ativo (`DRAFT_GENERATION_ENABLED=0`)
- [ ] `draft_kill_switch_active` exportado como gauge Prometheus quando kill switch ativo
- [ ] Trace OTel com `session_id` e `correlation_id` atravessando AudioGateway → STTPool
  (validado com mock OTel exporter)
- [ ] structlog emite JSON com `correlation_id`, `session_id`, `tenant_id` em todo log de
  processamento de Sessão
- [ ] Alerta Prometheus configurado para `deepgram_concurrent_sessions / deepgram_max_concurrent > 0.70`
- [ ] Migration Alembic com `downgrade()` obrigatório

### Blocked by
- #5
- #8

---

## 10) Isolamento de Tenant e Guardrails de CI
Tipo: AFK

### Context
Fase 2.0 opera com single-tenant (UUID default). Toda a contenção de isolamento existe
para garantir que a Fase 3.0 (multi-tenant operacional) não herde queries sem filtro de
tenant. Isolamento deve ser imposto por construção, não por convenção.

### What to build

**`TenantRepository` — padrão obrigatório:**
- Todo acesso ao banco passa por `TenantRepository` que injeta `WHERE tenant_id = :tid`
  via `SessionContext`
- Nenhum módulo importa `sqlalchemy.orm.Session` diretamente fora de `repositories/`
- Violação → lint CI falha (ver abaixo)

**Schema Postgres:**
- Toda tabela: `tenant_id UUID NOT NULL DEFAULT 'default-uuid'`
- Redis: hash tag `{tenant_id}:{session_id}` em toda chave de Sessão
  (ex: `session:{tenant_id}:{session_id}:events`)
- Métricas Prometheus: label `tenant_id="default"` em todas as métricas

**RLS (Row Level Security):**
- Policies **escritas** em todos os ambientes desde a 2.0
- Policies **desligadas** em produção (modo passivo)
- Policies em **shadow mode** em staging: `pgaudit` em log-only detecta queries que
  escapariam o filtro `WHERE tenant_id`
- Query escapada detectada em shadow staging → **bloqueia release**

**CI — lint de SQL:**
- `sqlfluff` com regra custom bloqueia `SELECT`, `UPDATE`, `DELETE` sem cláusula
  `tenant_id` em arquivos fora de `repositories/`
- Regra configurada em `.sqlfluff` no root do projeto

**Teste de isolamento cross-tenant:**
- Teste `test_tenant_isolation_cross_query`: cria dados para tenant A; query com
  `tenant_id=tenant_B` deve retornar vazio (não lançar exceção)
- Este teste entra no CI desde a 2.0 e roda em cada PR

**Redis — validação de hash tag:**
- Teste unitário valida que toda chave Redis gerada pelos módulos da Fase 2 contém
  `{tenant_id}` como hash tag

### Acceptance criteria
- [ ] `TenantRepository` injeta `WHERE tenant_id = :tid` em todos os SELECT/UPDATE/DELETE
  (verificado por teste de integração com banco em memória SQLite ou Postgres local)
- [ ] Import direto de `sqlalchemy.orm.Session` fora de `repositories/` → lint CI falha
- [ ] Toda tabela tem coluna `tenant_id NOT NULL DEFAULT 'default-uuid'` (verificado via
  reflexão Alembic)
- [ ] Toda chave Redis de Sessão contém hash tag `{tenant_id}` (teste unitário com mock
  Redis que valida formato de chave)
- [ ] `test_tenant_isolation_cross_query`: query com `tenant_id=B` em dados de `tenant_id=A`
  retorna vazio sem exceção
- [ ] `sqlfluff` com regra custom bloqueia `SELECT` sem `tenant_id` em arquivo fora de
  `repositories/` (testado com fixture de arquivo SQL violador)
- [ ] Métricas Prometheus contêm label `tenant_id="default"` (verificado no mock registry)
- [ ] Migration Alembic de RLS policies com `downgrade()` obrigatório

### Blocked by
- #1

---

## 11) Suite de Gate 2.0 — Definition of Done (9 Itens Obrigatórios)
Tipo: AFK

### Context
A Fase 2.0 só está concluída quando todos os 9 itens do DoD passarem de forma objetiva e
verificável. Esta issue implementa a suite de gate que executa todos os critérios e
retorna status binário `integravel: true|false`.

### What to build

**Os 9 itens obrigatórios do DoD 2.0:**

| # | Item | Critério de aceitação objetivo |
|---|---|---|
| 1 | E2E ponta a ponta com perfil `medical` | Criar Sessão + conectar WS + transcrever segmento de 30s + fechar + draft `ready` sem erro |
| 2 | Reconexão com dedupe | WS reconecta dentro de 5s; nenhum evento final duplicado nos últimos 10 eventos do Redis Stream |
| 3 | Draft materializado | `draft_status` em `{ready,pending,failed}` após `close`; `ready` para Sessão sem erro |
| 4 | Auth validada | JWT com RS256 + 7 claims obrigatórios aceito; ws_token válido → WS estabelecido; close revoga em < 2s |
| 5 | Fallback STT controlado | 3 timeouts mock → circuit abre; `stt_mode:fallback_batch` emitido; Sessão continua |
| 6 | `STT_CONFIDENCE` configurável | `STT_CONFIDENCE=0.45` → segmentos com confidence 0.40 marcados `uncertain=True` |
| 7 | Latência por bloco | Segmento de 10s de áudio → evento final STT em ≤ 15s (p95 em bateria de 20 execuções) |
| 8 | Cobertura campos `medical` | Draft de Sessão simulada com 8 campos de Anamnese → ≥ 90% dos campos preenchidos |
| 9 | Observabilidade de qualidade ativa | `draft_correction_ratio` e `events_late_arrival_ratio` exportados e visíveis no Prometheus |

**Suite de gate (`tests/fase2/test_gate_dod20.py`):**
```python
def test_gate_dod20():
    results = run_all_dod_checks()
    assert results["integravel"] is True, f"DoD 2.0 falhou: {results['failures']}"
```

**Relatório de evidências:** cada check gera `docs/gate/dod20_evidence_{timestamp}.json`
com status, métricas e logs relevantes — usado para decisão de avançar para Fase 2.1.

**Bateria WER (pré-gate):**
- 50 frases clínicas pt-BR fixas com Deepgram Nova-2
- WER calculado por Levenshtein sobre transcrição normalizada
- WER ≤ 10% → passa; > 10% → bloqueia go-live

**Bateria RED_FLAG:**
- Conjunto fixo de frases com red flags conhecidos (ex: "dor torácica irradiando para o
  braço", "perda de consciência súbita")
- Testado com Deepgram (web) e Whisper (Voice CLI)
- 100% de detecção obrigatória → falha bloqueia go-live

### Acceptance criteria
- [ ] `test_gate_dod20` passa somente quando todos os 9 checks passam individualmente
- [ ] `test_gate_dod20` falha com mensagem explícita dos checks que falharam (não apenas
  assert genérico)
- [ ] Evidência JSON gerada em `docs/gate/dod20_evidence_{timestamp}.json` a cada execução
- [ ] WER ≤ 10% na bateria de 50 frases clínicas com Deepgram Nova-2
- [ ] RED_FLAG detectado em 100% dos casos na bateria (Deepgram + Whisper)
- [ ] Cobertura ≥ 90% dos 8 campos obrigatórios da Anamnese no item #8
- [ ] Latência p95 ≤ 15s por bloco no item #7 (medida em 20 execuções com áudio sintético)
- [ ] Todos os testes de issues #1–#10 passam antes que esta suite seja executada

### Blocked by
- #2
- #3
- #4
- #5
- #6
- #7
- #8
- #9
- #10

---

## 12) Pacote HITL de Compliance como Operador — Go-Live
Tipo: HITL

### Context
Como Operador LGPD, o componente processa dados sensíveis (áudio e transcrição médica)
sob instrução do app de telemedicina (Controlador). Nenhum paciente real antes da
conclusão deste pacote. Lead time de DPA com providers é ≥ 60 dias — iniciar
imediatamente.

### What to build

**DPA com sub-operadores (bloqueador hard para produção real):**
- Deepgram: DPA incluindo cláusula de adequação para transferência internacional (Art. 33
  LGPD); Deepgram processa em US por padrão — cláusula de adequação ou mecanismo
  equivalente reconhecido pela ANPD é obrigatória
- OpenAI: DPA com cláusula equivalente
- Anthropic (Sonnet fallback): DPA com cláusula equivalente
- Declaração de sub-operadores (Deepgram, OpenAI, Anthropic) no DPA interno
  Controlador/Operador

**DPA interno (Controlador ↔ Operador):**
- Mesmo sendo a mesma empresa, o DPA interno formaliza a cadeia:
  *Controlador detém base legal Art. 11 §2 II "a" (tutela da saúde) → instrui Operador →
  Operador processa sob Art. 39 LGPD*
- Sem esse DPA o componente não pode tratar dados de saúde
- Documento em `docs/compliance/dpa-internal.md`

**DPIA do componente:**
- Finalidades: transcrição de consulta, extração de draft de prontuário, detecção de
  red flags
- Risco residual de imutabilidade do AuditLog: hash chain detecta adulteração mas DBA
  com acesso pode reescrever em cascade; **mitigações aceitas na 2.0:** (a) acesso DBA
  restrito a ≤ 3 pessoas nomeadas, (b) CloudTrail RDS registra toda operação privilegiada,
  (c) export diário de checkpoints do hash chain para S3 na 2.1 — risco residual deve ser
  **formalmente aceito e assinado no DPIA antes do primeiro paciente real**
- Retenção de áudio: efêmero por design (não persistido pelo componente)
- Retenção de transcrição: 6–12 meses
- Retenção de AuditLog: 5 anos
- Documento em `docs/compliance/dpia-componente.md`

**AuditLog (mínimo Postgres com hash chain):**
- Eventos cobertos: criação/encerramento de Sessão, geração de draft, exportação de draft,
  login (chamada de API com JWT/API key)
- Hash chain: `event.hash = SHA-256(event.payload || previous_event.hash)`
- Retenção: 5 anos
- Tabela `audit_log` com `downgrade()` na migration Alembic

**DPO indicado (Art. 41 LGPD):**
- DPO compartilhado com a empresa; nomeação formal registrada
- Notificação de breach ao Controlador em < 24h (Art. 39 LGPD) — runbook em
  `docs/runbooks/breach-notification.md`

**Checklist de readiness para go-live:**
- [ ] DPA Deepgram assinado com cláusula Art. 33 LGPD
- [ ] DPA OpenAI assinado
- [ ] DPA Anthropic assinado
- [ ] DPA interno Controlador/Operador assinado
- [ ] DPIA concluída com aceite formal de risco residual do AuditLog
- [ ] DPO indicado (Art. 41 LGPD) com nomeação formal
- [ ] AuditLog com hash chain implementado e testado (verificação de gap e hash chain
  quebrada)
- [ ] Runbook de notificação de breach em `docs/runbooks/breach-notification.md`
- [ ] Gate DoD 2.0 (#11) passou com evidências

### Acceptance criteria
- [ ] Teste de hash chain: inserir N eventos e verificar que `hash[i]` referencia
  `hash[i-1]` corretamente
- [ ] Teste de integridade: modificar `payload` de um evento e verificar que a verificação
  do hash chain detecta a adulteração
- [ ] Teste de gap: deletar um evento intermediário e verificar que a verificação detecta
  gap na cadeia
- [ ] AuditLog registra criação/encerramento/draft/exportação/login (teste com mock de
  Sessão completa)
- [ ] `docs/compliance/dpa-internal.md` existe e contém cadeia Controlador/Operador
- [ ] `docs/compliance/dpia-componente.md` existe com seção de risco residual do AuditLog
  e campo de aceite formal
- [ ] `docs/runbooks/breach-notification.md` existe com passos de notificação < 24h
- [ ] Checklist de readiness assinado (artefato físico/digital fora do repositório) antes
  de qualquer paciente real

### Blocked by
- #11
