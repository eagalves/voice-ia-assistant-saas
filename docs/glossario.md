# Glossário — Vocabulário Canônico do Projeto

Este glossário fixa os termos canônicos usados em todos os documentos (PRD, ADRs, arquitetura, issues) e no código. Ao escrever um novo doc ou módulo, **use estes termos exatos**. Quando uma palavra tem sinônimos comuns, o termo canônico é sempre o que aparece como cabeçalho da entrada.

## Domínio clínico

**Anamnese** — entrevista clínica estruturada de coleta inicial. No perfil `medical`, cobre 8 campos obrigatórios: queixa principal, início, duração, intensidade, sintomas associados, medicações em uso, alergias, sinais de alerta atuais. Lista canônica em `medical_rules.REQUIRED_FIELDS`.

**Consulta vs Sessão** — **Consulta** é o evento de negócio único: o encontro clínico com o paciente, com início, fim e produção de Prontuário. **Sessão** é a unidade técnica que implementa a Consulta no servidor; uma Consulta pode ter mais de uma Sessão (ex.: reconexão após queda de rede inicia nova Sessão técnica vinculada à mesma Consulta). Sessão é substituível dentro de uma Consulta; a Consulta é única e indivisível. Identificadores: `consultation_id` (estável durante toda a Consulta), `session_id` (UUID por Sessão técnica). Modos de Consulta no MVP: `consultation` (médico + paciente) e `doctor_followup` (médico sozinho refinando).

**Prontuário** — artefato textual estruturado emitido ao final da Consulta. Pode ser parcial (campos faltando, com aviso de pendências) ou final. No MVP é apenas texto; na Fase 2.2 ganha assinatura digital ICP-Brasil. Não confundir com transcrição (Prontuário é o output estruturado do assistente; transcrição é a fala bruta do médico+paciente).

**Sinal de alerta** (red flag) — termo clínico crítico que dispara protocolo de orientação para atendimento presencial/urgência. Lista versionada em `medical_rules.RED_FLAG_TERMS`. Usado pelo `MedicalProfileEngine` como gate obrigatório antes de emitir o Prontuário final.

**Participante** — pessoa identificada na conversa. Roles canônicos: `professional`, `patient`, `candidate`, `assistant`, `uncertain`. A atribuição por trecho de áudio é responsabilidade de `participant_attribution`.

**Revisão de Sessão** — etapa final obrigatória em perfis críticos (medical) onde trechos marcados como `uncertain` são confirmados antes de emitir o Prontuário. Implementação em `session_review`.

## Domínio de entrevista

**Entrevistador** — perfil que entrevista candidatos para vaga. Variantes: `general_interviewer`, `clinical_pharmacist_interviewer`, `clinical_nurse_interviewer`.

**Senioridade** — parâmetro de runtime do Entrevistador, ∈ `{junior, pleno, senior}`. Controla pesos de avaliação e profundidade técnica das perguntas. Configurada por env `SENIORITY_LEVEL` no MVP; por Sessão na Fase 2.0+.

**Ciclo de perguntas** — bloco de no máximo 3 perguntas antes de emitir resumo parcial. Implementação em `interview_flow`.

## Plataforma (Fase 2)

**Tenant** — organização cliente do SaaS (uma clínica, hospital, rede). Unidade de isolamento de dados, billing, perfis customizados e quotas. Identificado por `tenant_id` (UUID). Em UI pode aparecer como "Organização"; em código sempre `tenant`.

**Sessão** — instância única de uso do assistente, com início, fim, participantes, transcrição completa e artefato final. Identificada por `session_id` (UUID). Pertence a exatamente um Tenant. Persistida em Postgres na Fase 2.0+. Antes da Fase 2.0 a Sessão é apenas em RAM e descartada no encerramento.

**Médico** (operator) — usuário autenticado que opera o assistente durante a Sessão. Pertence a um Tenant. Login via OAuth/SSO da clínica.

**Paciente** — pessoa atendida na Consulta. Na Fase 2.0 não tem login (acesso por link assinado com token de Sessão); na 2.2 ganha consentimento explícito registrado por Sessão.

**Perfil ativo** — Profile YAML resolvido para uma Sessão específica. No MVP é boot-time global (`ASSISTANT_PROFILE`); na Fase 2.0+ é resolvido por Sessão; na 2.1+ é resolvido por `(tenant_id, session_kind)`.

**Profile resolver** — componente que dado o contexto retorna o `Profile` aplicável. Substitui a leitura direta de `ASSISTANT_PROFILE` do MVP.

**TenantContext** — objeto imutável que carrega `tenant_id`, `tenant_status`, `feature_flags` resolvidas e quotas do Tenant atual durante uma requisição. Injetado pelo `TenantResolver` middleware no início de toda requisição HTTP/WS e propagado por contexto async. Toda função de domínio que toque dados clínicos deve receber `TenantContext` explicitamente — passar `None` é erro de programação, não fallback.

**SessionContext** — objeto que carrega o estado de resolução de uma Sessão: `session_id`, `tenant_id` (UUID; pode ser o UUID default na Fase 2.0 single-tenant), `session_kind` (`consultation`, `doctor_followup`, etc.), `seniority` (entrevistas), `actor_role`. Passado ao `ProfileResolver` desde a Fase 2.0.

**TurnResult** — dataclass de saída do `VoiceOrchestrator.process_segment` (legacy) e `VoiceOrchestrator.process_stream` (Fase 2). Contém `ok`, `transcription`, `assistant_response`, `stt_status`, `stt_attempts`, `stt_confidence`, latências (`stt_latency_ms`, `llm_latency_ms`, `e2e_latency_ms`, `time_to_first_token_ms`) e `error_code` estável. Fail-soft: erros não propagam exceção; vêm em `error_code`.

**Prontuário draft** — versão exportável do Prontuário durante a Fase 2.0/2.1, **sem validade jurídica plena**. Marcado explicitamente como rascunho na UI e no PDF gerado. Vira documento legal apenas na Fase 2.2 com assinatura ICP-Brasil por evento. Antes da 2.2, não é evidência judicial; é ferramenta operacional.

**Diarização** — separação de quem-falou-o-quê dentro de um mesmo fluxo de áudio. Na Fase 2.0 fazemos diarização **por roteamento**: cada Participante conecta uma stream WS distinta (médico e paciente em navegadores separados) e o `role` é metadado da conexão. Diarização **por modelo** (uma stream com múltiplos falantes detectados pelo provider STT) entra na Fase 2.2 para Sessões presenciais. Não confundir com `ParticipantAttribution`, que é o módulo de domínio que mapeia `speaker_id` para `role` lógico.

**Endpointing** — detecção do fim de uma frase (silêncio + intenção) que aciona o turno final do STT streaming. No Deepgram Nova-2 é parâmetro do provider; configurável por Perfil. Diferente de VAD: VAD detecta presença de fala; endpointing detecta término de uma unidade semântica.

**Idempotency-key** — header obrigatório (`Idempotency-Key`) em endpoints que criam recursos com efeitos não reentrantes (ex.: `POST /sessions`, `POST /signatures`). Implementado via `Redis SET NX` com TTL de 24h. Requisição com mesma key retorna resposta original.

**Quota vs Rate limit** — **Rate limit** é proteção de curto prazo (req/s, tokens/min) e fail mode é `429 Too Many Requests`. **Quota** é alocação de recurso de longo prazo (minutos STT/dia/Tenant, tokens LLM/mês/Tenant) e fail mode é `403 Quota Exceeded` com upgrade path comercial. Ambos por Tenant. RateLimiter implementa os dois com mecanismos distintos (token bucket vs contador agregado).

## Técnico

**STT** (Speech-to-Text) — módulo que converte áudio em texto. Contrato: `STTService.transcribe(audio, sample_rate) -> TranscriptionResult`. Implementações: `LocalSTT` (Whisper, MVP); `ManagedSTT` (Deepgram / Google STT / OpenAI Whisper API, Fase 2.0+).

**LLM** (Large Language Model) — módulo que gera resposta textual. Contrato: `LLMService.stream(state, on_token) -> LLMResponse`. Implementações: `OpenAIBackend`, `GeminiBackend`, `OllamaBackend` (MVP).

**Backend** (LLM/STT) — implementação concreta de provider de IA. **Não confundir com "backend web"** — para o servidor HTTP/WebSocket usar o termo Server.

**Server** — processo HTTP/WebSocket que serve a aplicação web na Fase 2.0+. Termo preferido em vez de "backend".

**Audio gateway** — módulo da Fase 2.0+ que termina o WebSocket de áudio do navegador e alimenta o pipeline STT. Substitui `MicrophoneSource` para uso web. Voice CLI continua usando `MicrophoneSource` em ambiente desktop/dev.

**Session store** — Redis (estado quente de Sessão ativa) na Fase 2.0+; complementado por Postgres (transcrição persistida, Prontuário, auditoria) na 2.1+.

**TTFT** (Time to First Token) — latência entre envio do prompt e chegada do primeiro token de resposta do LLM em modo streaming. Métrica: `llm.ttft_ms`.

**WER** (Word Error Rate) — taxa de palavras com erro na transcrição STT. SLO: ≤ 10%.

**E2E latency** — tempo entre fim da fala e resposta textual completa exibida. Métrica: `e2e.latency_ms`. SLO: p95 < 4s.

**Stable error code** — código de erro de domínio independente de implementação (ex.: `LLM_ERR_UNAVAILABLE`, `STT_LOW_CONFIDENCE`, `PROFILE_NOT_FOUND`). Lista canônica em `errors.py` e nas seções de erro de cada módulo.

**Provider primário** — provider configurado em `LLM_PROVIDER`.

**Provider de fallback** — provider configurado em `LLM_FALLBACK_PROVIDER`. Dispara apenas em `LLM_ERR_UNAVAILABLE`.

## Compliance

**LGPD** — Lei Geral de Proteção de Dados (Lei 13.709/2018). Regula tratamento de dados pessoais. Dados clínicos são "sensíveis" (Art. 5º, II) e exigem base legal específica.

**CFM** — Conselho Federal de Medicina. Resoluções relevantes: 2.314/2022 (telemedicina), 1.821/2007 (prontuário eletrônico).

**Consentimento** — manifestação livre, informada, específica e inequívoca do titular concordando com tratamento de seus dados para finalidade determinada. **Não é base legal primária para dados clínicos** (essa é tutela da saúde). É base secundária para usos que vão além do estritamente clínico: gravação persistente de áudio, retenção além do mínimo, compartilhamento com providers, marketing. Capturado granularmente com cláusulas opt-in independentes versionadas, registradas por Sessão. Cláusula CFM 2.314 Art. 5º ("ambiente privado e adequado") é separada e bloqueia início se ausente.

**Tutela da saúde** — base legal LGPD Art. 11 §2 II "a" para tratamento de dados pessoais sensíveis em saúde. Base **primária** para Anamnese e Prontuário. Não exige consentimento (que é base secundária para usos não estritamente clínicos). Registrar `legal_bases:` por finalidade no Profile YAML.

**DPA** (Data Processing Agreement) — contrato entre Controlador (nós, Tenants) e Operador (providers Deepgram/OpenAI/Anthropic) regulando o tratamento de dados pessoais conforme LGPD Art. 39. **Bloqueador hard antes de mover Tenants reais para produção**. Auditado contratualmente: residência de dados, opt-out de uso para treinamento, sub-processadores, breach notification.

**Trilha de auditoria** (audit log) — registro imutável e ordenado de quem acessou qual recurso e quando. Implementação: tabela append-only em Postgres com WORM opcional. Retenção mínima: 5 anos.

**Retenção** — política de tempo máximo de armazenamento de dados pessoais por categoria. Após o limite, expurgo automático. Configurável por Tenant dentro de mínimos legais.

**Residência de dados** — exigência de que dados pessoais sensíveis sejam armazenados em infraestrutura no Brasil. Implica escolha de região cloud (`sa-east-1` ou equivalente).

**ICP-Brasil** — Infraestrutura de Chaves Públicas Brasileira (MP 2.200-2/2001). Padrão obrigatório para assinatura digital com validade jurídica plena no Brasil. Aplicado ao Prontuário final na Fase 2.2.

**DSR** (Data Subject Rights, LGPD Art. 18) — direitos do titular: acesso, correção, anonimização, eliminação, portabilidade, informação sobre compartilhamento. SLA legal: 15 dias.

**DPO** — Data Protection Officer (Encarregado, em LGPD). Pessoa designada como ponto de contato para titular e ANPD.

## Operação

**SLO** — Service Level Objective. Lista canônica do projeto:
- WER ≤ 10%
- E2E p95 < 4s
- Estabilidade: < 2% erro técnico em 30 min contínuos
- TTFT p95 < 2s (Fase 2.0+)

**RPO** (Recovery Point Objective) — quantidade máxima de dados que pode ser perdida em desastre, expressa em tempo de janela. Alvo na Fase 2.1: RPO 5 min (snapshot/replicação contínua de Postgres + Redis Streams persistidos).

**RTO** (Recovery Time Objective) — tempo máximo aceitável entre desastre e restauração de serviço. Alvo na Fase 2.1: RTO 1h. Validado por restore mensal em ambiente paralelo.

**Kill switch** — mecanismo de desativação imediata de um Tenant. Implementado por dois eixos: tabela `tenants(status ∈ {active, suspended, deleted})` (consultada na criação de Sessão e periodicamente em Sessões abertas) e flag Redis `KILL_SWITCH:{tenant_id}` para ação síncrona em < 1s. Acionável por: ordem judicial, fraude detectada, inadimplência > 7 dias, breach.

**Preset de latência** — bloco de configuração predefinido em `docs/latency_presets.md`: `baixa_latencia`, `balanceado`, `qualidade`.

**Fail-soft** — política do orquestrador: erros em qualquer estágio são capturados e devolvidos como `error_code` em `TurnResult`, mantendo a Sessão viva. Nunca propagar exceção até a UI.
