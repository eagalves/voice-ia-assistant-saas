# Arquitetura MVP - Assistente de Voz (Local + Escalavel)

## Objetivo
Construir um assistente **simples** para uso local que:
- escuta sua fala no microfone,
- transcreve com **STT local**,
- envia texto para **LLM via API key**,
- responde **apenas em texto**.

A base deve permitir troca futura de provedores (local ou API) com poucas alteracoes.

---

## Principios de Design
1. **Coesao alta por modulo**: cada modulo tem uma responsabilidade clara.
2. **Baixa interdependencia**: modulos conversam por contratos simples.
3. **Sem overengineering**: nada de microsservicos ou filas no MVP.
4. **Troca de provedor por configuracao**: sem reescrever fluxo principal.

---

## Escopo do MVP (Agora)
- Fonte de audio: microfone local.
- STT: local.
- LLM: remoto via API key.
- Saida: texto na tela.
- Memoria: historico curto da sessao.

Fora do escopo inicial:
- TTS (resposta em voz).
- Integracao com Teams/chamadas.
- Multiusuario.
- Deploy em nuvem.

---

## Modulos (coesos e com baixa interdependencia)

### 1) AudioInput
**Responsabilidade**
- Capturar audio do microfone local.
- Detectar inicio/fim de fala (VAD simples).

**Entradas**
- Configuracao de dispositivo de microfone.

**Saidas**
- Trechos de audio prontos para transcricao.

**Dependencias**
- Nenhuma dependencia dos modulos de IA.

---

### 2) STTAdapter
**Responsabilidade**
- Converter audio em texto.
- Expor resultado padronizado para o orquestrador.

**Contrato (conceitual)**
- Entrada: trecho de audio.
- Saida: texto transcrito, idioma (opcional), confianca (opcional).

**Implementacoes previstas**
- `LocalSTT` (agora): modelo local.
- `ApiSTT` (futuro): STT por key.

**Regra**
- O orquestrador conhece apenas o contrato STT, nunca a implementacao concreta.

---

### 3) ConversationState
**Responsabilidade**
- Manter contexto curto da sessao (ultimas mensagens).
- Controlar janela de contexto para limitar custo/token.

**Entradas**
- Mensagem do usuario (vinda do STT).
- Resposta do assistente (vinda do LLM).

**Saidas**
- Historico resumido para o LLM.

**Dependencias**
- Sem dependencia direta de AudioInput, STT ou LLM.

---

### 4) LLMAdapter
**Responsabilidade**
- Enviar prompt/contexto ao modelo e retornar resposta textual.

**Contrato (conceitual)**
- Entrada: prompt atual + historico de conversa.
- Saida: resposta em texto + metadados simples (tempo/tokens opcional).

**Implementacoes previstas**
- `ApiLLM` (agora): provider remoto via key.
- `LocalLLM` (futuro): modelo local.

**Regra**
- O orquestrador usa apenas o contrato LLM.

---

### 5) Orchestrator
**Responsabilidade**
- Fluxo ponta a ponta com regras simples:
  1. receber audio,
  2. pedir transcricao ao STT,
  3. atualizar estado da conversa,
  4. chamar LLM,
  5. devolver texto para UI,
  6. registrar metricas basicas.

**Dependencias**
- Depende de contratos (`STTAdapter`, `LLMAdapter`, `ConversationState`).
- Nao depende de implementacoes concretas.

---

### 6) TextUI
**Responsabilidade**
- Interface minima do usuario.
- Exibir status e historico textual.

**Tela minima**
- Botao iniciar/parar.
- Campo de transcricao ao vivo.
- Campo de resposta da IA.
- Status: ouvindo, transcrevendo, processando, erro.

**Dependencias**
- So conversa com o Orchestrator.

---

### 7) Config
**Responsabilidade**
- Centralizar configuracoes de ambiente.

**Chaves sugeridas**
- `STT_PROVIDER=local|api`
- `LLM_PROVIDER=api|local`
- `LLM_API_KEY=...`
- `LANG=pt-BR`
- `MAX_CONTEXT_MESSAGES=<numero>`

**Regra**
- Nenhuma key hardcoded no codigo.

---

### 8) Logger/Metrics (basico)
**Responsabilidade**
- Registrar eventos essenciais para operacao e evolucao.

**Metricas minimas**
- Latencia de STT.
- Latencia de LLM.
- Erros por modulo.
- Contagem de requisicoes.

**Regra**
- Sem logs com dados sensiveis completos.

---

## Dependencias entre Modulos (visao simples)
TextUI -> Orchestrator -> (STTAdapter, LLMAdapter, ConversationState, Logger)
AudioInput -> Orchestrator

Observacoes:
- `AudioInput` nao chama LLM.
- `STTAdapter` nao conhece UI.
- `LLMAdapter` nao conhece audio.
- Isso reduz acoplamento e facilita troca de tecnologia.

---

## Fluxo do MVP (runtime)
1. Usuario inicia escuta na UI.
2. AudioInput capta fala e gera trecho.
3. Orchestrator envia trecho ao STTAdapter.
4. STT retorna texto.
5. ConversationState atualiza historico.
6. Orchestrator chama LLMAdapter com contexto.
7. LLM retorna resposta textual.
8. UI mostra transcricao e resposta.

---

## Estrategia de Evolucao sem refatoracao pesada

### Fase A (agora)
- `STT_PROVIDER=local`
- `LLM_PROVIDER=api`
- Interface textual e fluxo unico.

### Fase B (futuro proximo)
- Adicionar `ApiSTT` mantendo mesmo contrato.
- Adicionar fallback de LLM provider por configuracao.

### Fase C (nuvem)
- Manter contratos.
- Substituir implementacoes locais por servicos quando preciso.
- Escalar Orchestrator sem mudar semantica dos modulos.

---

## Decisoes para evitar overengineering
1. Um processo/aplicacao unica no MVP.
2. Sem event bus/fila neste momento.
3. Sem banco complexo: memoria de sessao em RAM e opcional arquivo local simples.
4. Contratos pequenos e estaveis entre modulos.
5. Interface minimalista.

---

## Checklist de Qualidade do MVP
- [ ] STT local funcionando em pt-BR.
- [ ] LLM remoto funcionando com API key.
- [ ] Resposta textual ponta a ponta.
- [ ] Chaves em variavel de ambiente.
- [ ] Latencia e erros logados.
- [ ] Troca de provider por configuracao (sem mudar orquestrador).

---

## Resumo Executivo
Arquitetura enxuta: **AudioInput + STTAdapter + ConversationState + LLMAdapter + Orchestrator + TextUI + Config + Logger**.

Com isso, voce comeca local e simples hoje, mantendo a porta aberta para trocar STT/LLM entre local e API, e subir para nuvem depois sem reescrever o nucleo.

---

## Aditivo Pos-Issues (estado implementado)

Este aditivo registra evolucoes feitas apos a rodada base de issues 1-13.

### LLM providers atualmente suportados
- `LLM_PROVIDER=api|openai|gemini|ollama`.
- OpenAI, Gemini e Ollama coexistem no mesmo contrato de `LLMAdapter`/`LLMService`.
- O boot valida chave por provider remoto (`LLM_API_KEY` ou `GEMINI_API_KEY`) e regras de fallback.

### LLM local implementado
- Provider local: `LLM_PROVIDER=ollama`.
- Transporte: HTTP local para `OLLAMA_BASE_URL`.
- Formato: API de chat com mensagens estruturadas (`system`, `user`, `assistant`).
- Modelos separados por provider:
  - `OPENAI_MODEL`
  - `GEMINI_MODEL`
  - `OLLAMA_MODEL`
- Fallback opcional por chamada:
  - `LLM_FALLBACK_ENABLED=1`
  - `LLM_FALLBACK_PROVIDER=gemini|openai`
- Regra de fallback: apenas para erros tecnicos de disponibilidade do provider primario.
- Notificacao em desenvolvimento: exibir aviso de fallback quando `LLM_NOTIFY_FALLBACK=1`.
- Retries permanecem globais em `LLM_RETRIES`; timeout do Ollama fica especifico em `OLLAMA_TIMEOUT_S`.
- A resposta do servico expoe provider e modelo realmente usados.

### Proximos incrementos planejados
- Issue 19: testes de regressao para assertividade do perfil medical (anti-inferencia).
- Issue 20: encerramento limpo do worker de LLM no `voice_cli`.
- Issue 21: presets operacionais de latencia (local vs qualidade).

### Captura e transcricao em tempo real
- `AudioInput` recebeu `MicrophoneSource` com fallback de sample rate para melhorar compatibilidade no Windows.
- Foi adicionado um pipeline de streaming em `live_caption` com 3 threads:
  1. produtor de audio,
  2. segmentador VAD,
  3. worker de transcricao.
- O STT aceita bytes PCM e ndarray float32 (resample para 16 kHz no adaptador).

### Voice CLI operacional
- Existe entrypoint dedicado (`voice_cli`) para sessao de voz local.
- O fluxo STT final alimenta uma fila de requisicoes de LLM.
- Um worker dedicado processa o LLM em paralelo para nao bloquear captura/transcricao.

### Higiene de repositorio aplicada
- O artefato acidental de worktree em `.claude/worktrees/...` deixou de ser rastreado.
- O caminho foi adicionado ao `.gitignore` para evitar recorrencia.

---

## Perfil Medical MVP + SLOs Operacionais

### Perfil por configuracao
- Definir perfil do assistente por variavel de ambiente.
- Chave sugerida: `ASSISTANT_PROFILE=medical|general|sales`.
- No MVP, sem troca de perfil em runtime pela UI.

### Regras versionadas por perfil
- Manter regras em arquivos separados e versionados (exemplo: `profiles/medical.yaml`).
- Cada perfil possui contrato de saida obrigatorio proprio.

### Contrato obrigatorio de saida (perfil medical)
- Resumo do caso.
- Perguntas faltantes.
- Sinais de alerta.
- Hipoteses possiveis.
- Exames sugeridos para discutir com profissional.
- Orientacao final de seguranca.

### Coleta de anamnese (perfil medical)
- Campos obrigatorios iniciais: 8.
- Lista: queixa principal, inicio, duracao, intensidade, sintomas associados, medicacoes em uso, alergias, sinais de alerta atuais.
- Gerar prontuario parcial com aviso explicito de pendencias quando faltarem campos.
- Limitar a coleta a no maximo 3 perguntas por ciclo antes de emitir resumo parcial.

### Politica STT no MVP (seguranca e robustez)
- Priorizar precisao sobre latencia de modelo.
- Meta de latencia STT: p95 <= 2,5s por trecho.
- Limiar de confianca STT para bloqueio: 0,80.
- Abaixo do limiar: bloquear envio ao LLM e pedir repeticao.
- Maximo de 2 repeticoes por interacao.
- Apos 2 falhas: fallback para entrada manual por texto.
- Fallback manual com confirmacao curta obrigatoria antes de seguir.

### Politica de falhas (fail-soft)
- Realizar ate 2 tentativas com timeout curto por etapa.
- Em falha de STT: solicitar repeticao ao usuario.
- Em falha de LLM: exibir mensagem util e manter sessao ativa.
- Registrar erro por modulo para acompanhamento.

### SLO inicial objetivo (fase atual)
- STT: WER <= 10% (equivalente a cerca de 90% de acerto), medido em 50 frases fixas.
- Distribuicao recomendada das 50 frases: 30 em silencio e 20 com ruido leve.
- Latencia ponta a ponta: p95 < 4s (fim da fala ate resposta textual completa).
- Estabilidade: 30 minutos de uso continuo sem travar e taxa de erro tecnico < 2% por interacao.

### Sinais de alerta no Orchestrator
- Checagem obrigatoria de termos criticos antes do prontuario final.
- Quando houver sinal de alerta, incluir secao de alerta e orientacao explicita para atendimento presencial/urgencia.

---

## Como Trocar de Perfil

- A selecao de perfil no MVP e feita por variavel de ambiente `ASSISTANT_PROFILE`.
- Exemplos de perfil disponivel neste projeto:
  - `medical`
  - `clinical_pharmacist_interviewer`
  - `clinical_nurse_interviewer`
  - `general_interviewer`
- Para entrevistas, configure tambem `SENIORITY_LEVEL=junior|pleno|senior`.
- Referencias operacionais:
  - Guia de perfis: `profiles/README.md`
  - Exemplo de ambiente: `.env.example`
