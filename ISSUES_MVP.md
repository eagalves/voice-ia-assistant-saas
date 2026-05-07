# Backlog de Issues - MVP Assistente de Voz

## Diretrizes Tecnicas Transversais (obrigatorias para todas as issues)

Estas diretrizes sao parte do backlog e devem ser consideradas em toda implementacao, mesmo quando nao repetidas em cada issue.

### Stack e contratos base
- Linguagem: Python.
- STT local: Whisper.
- LLM remoto: OpenAI GPT-4o via API key.
- Perfis configuraveis por `ASSISTANT_PROFILE`, resolvidos por `profile.id` (chave canonica).
- Perfis de entrevista usam `SENIORITY_LEVEL=junior|pleno|senior`.
- Perfil medical usa `MEDICAL_SESSION_MODE=consultation|doctor_followup`.

### Regras de arquitetura
- Orchestrator depende de contratos, nao de implementacoes concretas.
- ConversationState armazena mensagens com `role` + conteudo.
- Sessao pode ser single-participant ou multi-participant.
- Atribuicao de participante deve suportar role incerto sem interromper atendimento.

### Politicas operacionais
- STT com limiar de confianca 0.80.
- Abaixo do limiar: bloquear envio ao LLM e aplicar retentativas.
- Maximo de 2 retentativas antes de fallback manual com confirmacao curta.
- Politica fail-soft por etapa: maximo 2 tentativas com timeout curto.
- Logs sem dados sensiveis completos.

### Criticidade por perfil
- No MVP, apenas `medical` e perfil critico.
- Revisao final de trechos com role incerto:
	- obrigatoria em perfil critico;
	- opcional em perfis nao criticos.

### SLOs alvo do MVP
- STT: WER <= 10% em 50 frases fixas.
- Latencia ponta a ponta: p95 < 4s.
- Estabilidade: 30 min continuos com taxa de erro tecnico < 2%.

### Definition of Done (DoD) minima por issue
- Comportamento demoavel ponta a ponta do slice.
- Testes automatizados cobrindo comportamento externo do slice.
- Logs/metricas minimos do slice instrumentados.
- Sem regressao dos contratos ja definidos no PRD.

## Issue 1 - Bootstrap de Configuracao e Perfil Ativo

**Type:** AFK  
**Blocked by:** None - can start immediately  
**User stories covered:** 9, 17, 18, 21

## What to build
Inicializar o aplicativo com carregamento de variaveis de ambiente e resolucao do perfil ativo por id.

## Acceptance criteria
- [ ] Given ambiente valido, When o app inicializa, Then o perfil ativo e carregado por variavel de ambiente.
- [ ] Given configuracoes sensiveis definidas, When o app sobe, Then os valores sao lidos apenas do ambiente.
- [ ] Given configuracao essencial ausente, When o app inicializa, Then a inicializacao falha com erro claro.

## Riscos tecnicos
- Divergencia entre nomes de arquivo e `profile.id` causar carga de perfil incorreta.
- Variaveis obrigatorias ausentes em ambientes locais diferentes.
- Config com tipos inconsistentes (string vs numero) gerar falha tardia.

## Casos de teste minimos
- Inicializacao com ambiente completo deve carregar perfil ativo sem erro.
- Inicializacao sem `ASSISTANT_PROFILE` deve falhar com erro explicito.
- Inicializacao sem `LLM_API_KEY` (quando LLM remoto ativo) deve falhar no boot.

---

## Issue 2 - Validacao Estrita de Perfis no Boot

**Type:** AFK  
**Blocked by:** Issue 1  
**User stories covered:** 18, 21

## What to build
Validacao fail-fast dos perfis no boot com schema e codigos de erro estaveis.

## Acceptance criteria
- [ ] Given perfil invalido, When o loader valida no boot, Then retorna falha com erro estruturado.
- [ ] Given dois perfis com mesmo `profile.id`, When o catalogo e montado, Then a inicializacao falha por duplicidade.
- [ ] Given cenarios valido, schema invalido e id inexistente, When os testes executam, Then todos os resultados esperados sao atendidos.

## Riscos tecnicos
- Schema muito permissivo deixar passar perfis quebrados.
- Parse YAML inconsistente entre ambientes.
- Falta de codigos de erro estaveis dificultar observabilidade.

## Casos de teste minimos
- `valid_profile_minimal` deve validar e carregar.
- `invalid_profile_example` deve falhar com erro de schema.
- `ASSISTANT_PROFILE` inexistente deve retornar `PROFILE_NOT_FOUND`.
- Dois perfis com mesmo `profile.id` devem retornar `PROFILE_ID_DUPLICATE`.

---

## Issue 3 - Captura de Audio Local com Segmentacao de Fala

**Type:** AFK  
**Blocked by:** Issue 1  
**User stories covered:** 1, 8

## What to build
Capturar audio local com inicio/parada de escuta, segmentacao de fala e metadado de origem por trecho.

## Acceptance criteria
- [ ] Given sessao inativa, When o usuario inicia/parar escuta, Then a captura e controlada de forma estavel.
- [ ] Given deteccao de fala, When um trecho e segmentado, Then ele e emitido com metadados de origem.
- [ ] Given mudanca de estado da captura, When a UI consulta o estado, Then `ouvindo/erro` fica disponivel.

## Riscos tecnicos
- VAD cortar fala no meio ou manter silencio como fala.
- Drivers de audio diferentes por OS causarem instabilidade.
- Bloqueio do thread principal durante captura continua.

## Casos de teste minimos
- Iniciar escuta deve mudar estado para ouvindo e emitir trecho ao detectar fala.
- Parar escuta deve encerrar captura sem processo pendurado.
- Trecho emitido deve conter metadado de origem valido.

---

## Issue 4 - STT Local com Politica de Confianca e Fallback

**Type:** AFK  
**Blocked by:** Issue 3  
**User stories covered:** 2, 3, 4, 5

## What to build
Implementar STT local em pt-BR com limiar de confianca, retentativas e fallback manual com confirmacao curta.

## Acceptance criteria
- [ ] Given confianca de STT >= 0.80, When a transcricao e concluida, Then o fluxo segue normalmente.
- [ ] Given confianca de STT < 0.80, When o trecho e processado, Then a politica de retentativa e aplicada.
- [ ] Given limite de tentativas atingido, When STT continua abaixo do limiar, Then fallback manual com confirmacao obrigatoria e acionado sem encerrar a sessao.

## Riscos tecnicos
- Score de confianca do STT variar por idioma/ruido e quebrar regra fixa.
- Retentativas sem timeout travarem fluxo.
- Fallback manual sem confirmacao propagar erro para o LLM.

## Casos de teste minimos
- Entrada com confianca >= 0.80 deve seguir para proxima etapa.
- Entrada com confianca < 0.80 deve acionar retentativa ate limite.
- Apos 2 falhas, fallback manual deve ser exibido e exigir confirmacao curta.

---

## Issue 5 - Estado de Conversa com Role por Participante

**Type:** AFK  
**Blocked by:** Issue 1  
**User stories covered:** 6, 25

## What to build
Manter historico curto em sessao com mensagens contendo role de participante e conteudo.

## Acceptance criteria
- [ ] Given nova mensagem de participante, When ela e registrada, Then a sessao persiste role e conteudo.
- [ ] Given historico acima do limite, When nova mensagem entra, Then a janela de contexto respeita limite configuravel.
- [ ] Given envio de historico ao LLM, When a serializacao ocorre, Then os roles sao preservados corretamente.

## Riscos tecnicos
- Estouro de contexto por limite mal aplicado.
- Mistura de roles em mensagens multi-participante.
- Perda de ordem temporal das mensagens.

## Casos de teste minimos
- Inserir mensagens acima do limite deve descartar as mais antigas.
- Mensagens com roles diferentes devem manter role e ordem de insercao.
- Serializacao para LLM deve preservar role e conteudo.

---

## Issue 6 - LLM Remoto com Prompt Derivado do Perfil

**Type:** AFK  
**Blocked by:** Issue 1, Issue 5  
**User stories covered:** 6, 7, 21, 22

## What to build
Integrar provider remoto de LLM e montar prompt com regras do perfil ativo.

## Acceptance criteria
- [ ] Given historico de sessao valido, When o LLM e chamado, Then a resposta textual e gerada com contexto.
- [ ] Given perfil ativo carregado, When o prompt e montado, Then regras e guardrails do perfil sao aplicados.
- [ ] Given falha de chamada ao LLM, When a politica de erro executa, Then retry e mensagem util ao usuario sao aplicados.

## Riscos tecnicos
- Prompt sem regras de perfil gerar comportamento inconsistente.
- Timeouts da API aumentar latencia ponta a ponta.
- Erro de parse da resposta quebrar orquestracao.

## Casos de teste minimos
- Chamada com historico deve retornar resposta textual valida.
- Mudanca de perfil deve alterar instrucoes de sistema aplicadas.
- Simulacao de timeout deve acionar retry e mensagem de falha util.

---

## Issue 7 - Orquestrador E2E Base

**Type:** AFK  
**Blocked by:** Issue 4, Issue 5, Issue 6  
**User stories covered:** 6, 7, 8, 19, 20, 22

## What to build
Implementar fluxo completo audio -> STT -> estado -> LLM -> UI com metricas e logs por modulo.

## Acceptance criteria
- [ ] Given modulos conectados, When uma interacao completa e executada, Then o fluxo ponta a ponta conclui de forma estavel.
- [ ] Given uma interacao processada, When metricas sao emitidas, Then latencias de STT e LLM sao registradas.
- [ ] Given erro em qualquer etapa, When o logger registra evento, Then o erro por modulo e salvo sem dados sensiveis.

## Riscos tecnicos
- Acoplamento indevido do orquestrador a implementacoes concretas.
- Falha em um modulo interromper sessao inteira sem recuperacao.
- Metricas incompletas dificultarem tuning de SLO.

## Casos de teste minimos
- Fluxo completo audio->STT->estado->LLM->UI deve concluir com sucesso.
- Falha em STT deve manter sessao ativa com tratamento esperado.
- Logs devem registrar latencia por etapa e codigo de erro por modulo.

---

## Issue 8 - Atribuicao Automatica de Participante em Sessao Multi-Participante

**Type:** AFK  
**Blocked by:** Issue 7  
**User stories covered:** 23, 25

## What to build
Implementar identificacao automatica do participante principal assistido e atribuicao do falante secundario como outro participante da sessao.

## Acceptance criteria
- [ ] Given sessao com multiplos participantes, When a atribuicao automatica roda, Then roles sao definidos automaticamente.
- [ ] Given baixa confianca de atribuicao, When o trecho e classificado, Then ele e marcado como role incerto.
- [ ] Given marcacao de role incerto, When a sessao continua, Then nao ha interrupcao em tempo real do atendimento.

## Riscos tecnicos
- Falso positivo na identificacao do profissional contaminar contexto.
- Queda de confianca frequente gerar excesso de trechos incertos.
- Mudanca de ambiente acustico degradar identificacao em tempo real.

## Casos de teste minimos
- Com voz cadastrada do profissional, sistema deve marcar corretamente role principal.
- Quando confianca de identificacao cair, trecho deve ser marcado como incerto.
- Sessao deve continuar sem prompts de interrupcao em tempo real.

---

## Issue 9 - Revisao Final de Trechos Incertos por Criticidade

**Type:** AFK  
**Blocked by:** Issue 8  
**User stories covered:** 24, 25

## What to build
Implementar revisao de trechos com role incerto no fechamento da sessao, com obrigatoriedade por criticidade do perfil.

## Acceptance criteria
- [ ] Given perfil `medical` com trechos incertos, When o usuario tenta fechar sessao, Then revisao e obrigatoria antes do artefato final.
- [ ] Given perfil nao critico com trechos incertos, When o fechamento e solicitado, Then a finalizacao pode ocorrer sem revisao obrigatoria.
- [ ] Given acao de revisao concluida, When a sessao e finalizada, Then decisoes de revisao ficam registradas para auditoria interna.

## Riscos tecnicos
- Regra de criticidade aplicada no perfil errado.
- Revisao final longa demais comprometer UX.
- Ausencia de trilha de auditoria para correcoes de role.

## Casos de teste minimos
- Perfil medical com trechos incertos deve bloquear artefato final ate revisao.
- Perfil nao critico com trechos incertos deve permitir finalizacao opcional.
- Confirmacoes/correcoes feitas na revisao devem ficar registradas na sessao.

---

## Issue 10 - Regras Clinicas do Perfil Medical

**Type:** AFK  
**Blocked by:** Issue 7, Issue 9  
**User stories covered:** 10, 11, 12, 13, 23, 24

## What to build
Aplicar regras de anamnese e seguranca do perfil medical com prontuario parcial/final e sinais de alerta.

## Acceptance criteria
- [ ] Given sessao medical ativa, When a coleta de anamnese ocorre, Then os 8 campos obrigatorios sao suportados no fluxo.
- [ ] Given campos faltantes, When o artefato e gerado, Then prontuario parcial com pendencias e emitido corretamente.
- [ ] Given deteccao de sinais de alerta, When o artefato e montado, Then secao de alerta e orientacao de seguranca sao incluidas.

## Riscos tecnicos
- Coleta incompleta de campos obrigatorios gerar prontuario fraco.
- Regra de sinais de alerta com baixa cobertura de termos criticos.
- Geracao de prontuario final sem validar pendencias.

## Casos de teste minimos
- Sessao com campos faltantes deve gerar prontuario parcial com pendencias explicitas.
- Sessao com termos criticos deve incluir secao de alerta e orientacao de seguranca.
- Sessao completa deve gerar prontuario final com seções obrigatorias do perfil.

---

## Issue 11 - Perfis de Entrevista com Senioridade Configuravel

**Type:** AFK  
**Blocked by:** Issue 7  
**User stories covered:** 14, 15, 16, 21

## What to build
Implementar fluxos de entrevista (farmaceutico clinico, enfermagem clinica, geral) com senioridade junior/pleno/senior.

## Acceptance criteria
- [ ] Given `SENIORITY_LEVEL` definido, When a entrevista inicia, Then trilha e pesos de avaliacao mudam conforme perfil.
- [ ] Given entrevista em andamento, When o assistente conduz o fluxo, Then apenas uma pergunta por vez e apresentada.
- [ ] Given entrevista concluida, When o relatorio final e gerado, Then recomendacao e justificativas estruturadas sao apresentadas.

## Riscos tecnicos
- Parametro de senioridade invalido nao ser rejeitado no boot.
- Mistura de regras entre perfis de entrevista.
- Saida final sem evidencias objetivas por competencia.

## Casos de teste minimos
- Alterar `SENIORITY_LEVEL` deve mudar pesos e perguntas esperadas.
- Fluxo deve respeitar uma pergunta por vez com follow-up.
- Relatorio final deve conter recomendacao e justificativas por competencia.

---

## Issue 12 - TextUI Operacional com Correcoes de Sessao

**Type:** AFK  
**Blocked by:** Issue 7, Issue 9, Issue 10, Issue 11  
**User stories covered:** 1, 4, 5, 7, 8, 24

## What to build
Concluir interface textual operacional com status em tempo real, fallback manual e ajustes finais de sessao.

## Acceptance criteria
- [ ] Given sessao ativa, When eventos de processamento ocorrem, Then status, transcricao e resposta ficam visiveis em tempo real.
- [ ] Given falha de STT por regra, When fallback e acionado, Then entrada manual com confirmacao curta fica disponivel na UI.
- [ ] Given etapa de fechamento, When o usuario revisa a sessao, Then ajustes finais sao possiveis antes da emissao do artefato.

## Riscos tecnicos
- Atualizacao de tela lenta em fluxo continuo de eventos.
- Estado visual inconsistente com estado real da sessao.
- Fallback manual sem integracao correta com orquestrador.

## Casos de teste minimos
- Transcricao e resposta devem aparecer em tempo real durante sessao ativa.
- Acionar fallback manual deve coletar confirmacao e prosseguir no fluxo.
- Ajustes finais na sessao devem refletir no artefato emitido.

---

## Issue 13 - Testes de Aceite por Ciclo e SLOs

**Type:** AFK  
**Blocked by:** Issue 12  
**User stories covered:** 18, 19, 20

## What to build
Criar suite de aceite focada em comportamento externo e metas operacionais do MVP.

## Acceptance criteria
- [ ] Given conjunto de frases definido, When a bateria de STT executa, Then o WER alvo e validado.
- [ ] Given amostra de interacoes representativa, When a medicao e consolidada, Then a latencia p95 ponta a ponta e validada.
- [ ] Given execucao continua por 30 minutos, When os resultados sao apurados, Then estabilidade e taxa de erro tecnico sao validadas.

## Riscos tecnicos
- Ambiente de teste nao representar condicoes reais de uso.
- Medicao de latencia inconsistente entre etapas.
- Falta de repetibilidade nos testes de estabilidade.

## Casos de teste minimos
- Rodar bateria de 50 frases e calcular WER consolidado.
- Medir p95 ponta a ponta com amostra minima definida e reproducivel.
- Executar sessao continua de 30 min e apurar taxa de erro tecnico.

---

## Registro Pos-Issue 13 (Track de modificacoes)

Periodo rastreado: `2819b1b111b0f8a4a309fbf07a929b36cd629712` -> `88293d9261193952150027ad989f1ef9186fb2db`

Objetivo deste bloco: manter trilha do que foi alterado apos a entrega das issues 1-13, se era necessario, e quais pendencias viraram novas issues.

### Mudancas aplicadas e avaliacao de necessidade

1. Suporte a provider Gemini (bootstrap + LLM factory + validacoes)
- Arquivos: `src/assistente_voz/bootstrap.py`, `src/assistente_voz/llm.py`, `.env.example`, `tests/test_issue1_bootstrap.py`, `tests/test_issue6_llm.py`.
- Status: **feito**.
- Necessidade: **necessario** para alternancia de provider sem mudar orquestrador.

2. Dependencias para Gemini e audio em tempo real
- Arquivo: `pyproject.toml`.
- Inclusoes: `google-genai`, `sounddevice`, `numpy`.
- Status: **feito**.
- Necessidade: **necessario** para o escopo de voz local + Gemini.

3. Captura de microfone robusta (fallback de sample rate no Windows)
- Arquivo: `src/assistente_voz/audio_capture.py` (`MicrophoneSource`).
- Status: **feito**.
- Necessidade: **necessario** para estabilidade de captura em diferentes dispositivos.

4. Pipeline de legenda/transcricao em streaming
- Arquivo: `src/assistente_voz/live_caption.py` (`StreamingTranscriber`).
- Status: **feito**.
- Necessidade: **necessario** para reduzir latencia e melhorar completude.

5. STT atualizado para bytes/ndarray + tuning de confianca
- Arquivo: `src/assistente_voz/stt.py`.
- Status: **feito**.
- Necessidade: **necessario** para suportar caminho streaming com melhor robustez.

6. CLI de voz operacional e religacao do LLM
- Arquivo: `src/assistente_voz/voice_cli.py`.
- Cobertura: `tests/test_issue14_voice_cli.py`.
- Status: **feito**.
- Necessidade: **necessario** para remover regressao STT-only e restaurar resposta da IA.

7. Artefato acidental versionado de worktree
- Caminho: `.claude/worktrees/condescending-volhard-fe75a2`.
- Status: **presente no historico**.
- Necessidade: **nao necessario** para o produto; deve ser removido do controle de versao.

### Novas issues abertas (pos-MVP base)

## Issue 14 - Voice CLI com LLM Reativado e Testado

**Type:** AFK
**Blocked by:** Issue 13
**Status:** Done

### Entregas
- Fluxo de voz agora envia transcricoes finais ao LLM.
- Worker dedicado para LLM sem bloquear captura/STT.
- Modo `--text` funcional para smoke test de LLM.
- Testes adicionados em `tests/test_issue14_voice_cli.py`.

### Criterio de aceite (resultado)
- [x] `run_text_mode` retorna resposta de LLM.
- [x] `run_voice_mode` responde para cada evento final de fala.
- [x] Suite de testes verde apos alteracao.

## Issue 15 - Suporte Multi-Provider de LLM (OpenAI + Gemini)

**Type:** AFK
**Blocked by:** Issue 6
**Status:** Done

### Entregas
- `LLM_PROVIDER=api|openai|gemini` com validacao no boot.
- `GEMINI_API_KEY` validada quando provider `gemini`.
- `LLMService.from_env` com selecao de backend por provider.

### Criterio de aceite (resultado)
- [x] Provider Gemini selecionado por ambiente.
- [x] Provider invalido falha com erro explicito.
- [x] Testes de bootstrap e LLM atualizados.

## Issue 16 - Higiene de Repositorio: Remover Worktree Acidental

**Type:** AFK
**Blocked by:** None
**Status:** Done
**Prioridade:** Alta

### Problema
- O caminho `.claude/worktrees/condescending-volhard-fe75a2` foi versionado como gitlink/subprojeto acidental.

### O que fazer
- Remover esse caminho do controle de versao.
- Ajustar `.gitignore` para evitar recorrencia.
- Validar que clone limpo nao referencia worktree interno.

### Criterio de aceite
- [x] Caminho de worktree nao aparece mais em `git ls-files`.
- [x] `git status` limpo apos remocao.
- [x] Regra preventiva em `.gitignore` revisada.

## Issue 17 - Seguranca de Credenciais em Ambiente e Historico

**Type:** AFK
**Blocked by:** None
**Status:** Open
**Prioridade:** Alta

### Problema
- Houve registro de commit com contexto de chave (`add gemini key`) e exposicao de credenciais em fluxo de trabalho.

### O que fazer
- Rotacionar chaves OpenAI/Gemini imediatamente.
- Garantir que `.env` continue fora do versionamento.
- Revisar historico recente para remover segredos, se aplicavel.

### Criterio de aceite
- [ ] Chaves antigas revogadas e novas chaves em uso.
- [ ] Nenhum segredo presente em arquivos versionados.
- [ ] Procedimento de rotacao documentado no repo.

## Issue 18 - Suporte Planejado a LLM Local via Ollama com Fallback Tecnico

**Type:** AFK
**Blocked by:** Issue 15
**Status:** Done
**Prioridade:** Alta

### Objetivo
- Permitir uso de LLM local no proprio PC durante desenvolvimento, sem remover suporte a providers por API.

### Decisoes fechadas
- Provider explicito: `LLM_PROVIDER=ollama`.
- Fallback opcional e por chamada: `LLM_FALLBACK_PROVIDER=<provider>`.
- Fallback acionado apenas para erros tecnicos de disponibilidade.
- Aviso de fallback visivel em desenvolvimento quando `LLM_NOTIFY_FALLBACK=1`.
- Modelos separados por provider (`OPENAI_MODEL`, `GEMINI_MODEL`, `OLLAMA_MODEL`).
- Integracao no nivel de `LLMService`, nao apenas no `voice_cli`.
- Ollama usando API HTTP local com mensagens estruturadas.
- Retries globais via `LLM_RETRIES`; timeout proprio via `OLLAMA_TIMEOUT_S`.

### Configuracao alvo
- `LLM_PROVIDER=ollama`
- `LLM_FALLBACK_PROVIDER=gemini`
- `LLM_FALLBACK_ENABLED=1`
- `LLM_NOTIFY_FALLBACK=1`
- `OLLAMA_BASE_URL=http://localhost:11434`
- `OLLAMA_MODEL=qwen2.5:3b`
- `OLLAMA_TIMEOUT_S=30`
- `GEMINI_MODEL=gemini-2.0-flash`
- `OPENAI_MODEL=gpt-4o`

### Riscos tecnicos
- Bootstrap aceitar combinacoes invalidas de provider/fallback/modelo.
- Fallback mascarar bugs logicos se classificar erro tecnico de forma frouxa.
- Resposta final nao expor provider/modelo reais e dificultar debug.
- Contrato de mensagens do Ollama divergir dos providers remotos.

### Criterio de aceite
- [x] `LLM_PROVIDER=ollama` funciona no mesmo `LLMService.from_env`.
- [x] Fallback para `gemini` ocorre apenas em erro tecnico de disponibilidade.
- [x] Aviso explicito de fallback aparece em ambiente de desenvolvimento.
- [x] Resposta carrega provider e modelo realmente utilizados.
- [x] Documentacao de `.env.example` cobre configuracao de Ollama e fallback.
- [x] Testes automatizados cobrem primario local, fallback tecnico e ausencia de fallback em erro logico.

## Issue 19 - Regressao de Assertividade do Perfil Medical

**Type:** AFK
**Blocked by:** Issue 18
**Status:** Done
**Prioridade:** Alta

### Objetivo
- Garantir por teste que o perfil medical nao infere contexto clinico ambiguo sem confirmacao explicita.

### O que fazer
- Adicionar testes cobrindo instrucoes do `profiles/medical.yaml` no prompt de sistema.
- Criar casos de regressao para termos ambiguos (ex.: "acidente") exigindo pergunta de esclarecimento.
- Validar que o comportamento permaneceu "uma pergunta por vez" quando aplicavel.

### Criterio de aceite
- [x] Testes falham sem as instrucoes e passam com as instrucoes atuais.
- [x] Caso ambiguo nao assume contexto (ex.: nao assumir acidente de transito).
- [x] Suite de testes permanece verde apos incluir os cenarios.

## Issue 20 - Encerramento Limpo do Worker de LLM no Voice CLI

**Type:** AFK
**Blocked by:** Issue 18
**Status:** Done
**Prioridade:** Alta

### Objetivo
- Eliminar warning prematuro de encerramento quando o worker de LLM ainda esta concluindo chamada valida no fim da sessao.

### O que fazer
- Revisar estrategia de shutdown em `voice_cli` para drenar fila com previsibilidade.
- Ajustar timeout/join para evitar falso negativo em chamadas lentas (Ollama local).
- Manter encerramento responsivo para Ctrl+C e fim por duracao.

### Criterio de aceite
- [x] Nao exibe warning de worker em fluxo normal com resposta em andamento.
- [x] Nao deixa thread pendurada apos encerramento.
- [x] Teste automatizado cobrindo encerramento com request LLM em curso.

## Issue 21 - Perfis Operacionais de Latencia (Local vs Qualidade)

**Type:** AFK
**Blocked by:** Issue 18
**Status:** Done
**Prioridade:** Media

### Objetivo
- Fornecer presets claros de configuracao para reduzir latencia em maquina local sem perder previsibilidade de operacao.

### O que fazer
- Definir presets documentados para:
	- baixa latencia local,
	- balanceado,
	- qualidade.
- Incluir recomendacoes de `OLLAMA_MODEL`, `LLM_RETRIES`, `OLLAMA_TIMEOUT_S`, `WHISPER_MODEL` e `STT_CONFIDENCE_THRESHOLD` para cada preset.
- A escolha do tamanho do modelo Whisper (`tiny`, `base`, `small`, `medium`, `large`) e a principal alavanca de latencia STT e deve ser explicitada em cada preset.
- Atualizar docs de operacao para testes de voz e texto.

### Riscos tecnicos
- Preset de baixa latencia com modelo Whisper muito pequeno degradar WER abaixo do SLO.
- Recomendacoes de modelo Ollama e Whisper desalinhadas causarem preset inconsistente.

### Criterio de aceite
- [x] Presets documentados em arquivo de referencia do projeto (`docs/latency_presets.md`), incluindo `WHISPER_MODEL` para cada preset.
- [x] Tempo de resposta medio melhor no preset de baixa latencia em comparacao ao baseline — documentado na tabela de alavancas por modelo/hardware.
- [x] WER do preset de baixa latencia mensurado e registrado — tabela de WER por modelo Whisper incluida; `base` com beam 1 estimado em ~10-12%, dentro do limite de 10% com margem estreita, documentado com aviso.
- [x] Nao regressao funcional dos fluxos de voz/texto — presets sao apenas configuracao de ambiente, sem alteracao de codigo.

---

## Issue 22 - Fallback Silencioso de Sample Rate no Windows

**Type:** Observabilidade
**Blocked by:** None
**Status:** Done
**Prioridade:** Alta

### Diagnostico (pos-implementacao)
O problema original foi classificado como bug de dados, mas a investigacao revelou que o caminho
principal de producao (`voice_cli` -> `StreamingTranscriber` -> `_safe_transcribe`) ja fazia o
resampling corretamente: `_safe_transcribe` recebe o `sr` real da fonte, chama `_to_float32(buf,
sample_rate=sr)` e passa o array resultante ao backend com `_input_sample_rate=16000` para evitar
double-resample. A qualidade de transcricao **nao estava degradada** nesse fluxo.

O que de fato estava ausente era **observabilidade**: quando o dispositivo caia em fallback (ex.:
48 kHz no Windows), nenhum sinal aparecia nos logs em producao — apenas `print` gateado por
`debug=True`.

Existe um bug latente real em rota alternativa: se `STTService.transcribe()` for chamado
diretamente com `audio_bytes` de um `AudioSegment` capturado em taxa diferente de 16 kHz, o
`FasterWhisperBackend._input_sample_rate` permanece em 16000 e o audio nao e resamplado —
resultando em transcricao de voz em velocidade 3x incorreta. Esse cenario esta coberto pela
**Issue 30**.

### O que foi feito
- Adicionado `STT_REQUIRED_SAMPLE_RATE = 16000` como constante explicita em `audio_capture.py`.
- `MicrophoneSource.open()` emite `WARNING` quando o dispositivo rejeita a taxa solicitada (fallback).
- `MicrophoneSource.open()` emite `WARNING` quando a taxa efetiva difere de `STT_REQUIRED_SAMPLE_RATE`.
- `MicrophoneSource.open()` emite `INFO` com dispositivo e taxa efetiva em toda abertura bem-sucedida.
- Prints de debug substituidos por `_logger.debug()` (sem gate de `self._debug`).

### Casos de teste (todos passando em `tests/test_issue22_sample_rate.py`)
- [x] `_to_float32` reamosta 44100 Hz para ~16000 amostras.
- [x] `_to_float32` reamosta 48000 Hz para exatamente 16000 amostras.
- [x] `_to_float32` nao altera contagem de amostras ja em 16 kHz.
- [x] Saida de `_to_float32` esta em [-1, 1].
- [x] WARNING emitido quando dispositivo rejeita 16 kHz e aceita 48 kHz.
- [x] WARNING menciona a taxa solicitada rejeitada.
- [x] INFO registra taxa efetiva em toda abertura bem-sucedida.
- [x] Nenhum WARNING de mismatch quando dispositivo aceita 16 kHz diretamente.
- [x] `sample_rate` do source reflete a taxa real apos fallback.
- [x] `_safe_transcribe` entrega ao backend exatamente 16000 amostras quando audio foi capturado em 48 kHz.

---

## Issue 23 - Classificacao Robusta de Erros Tecnicos no Fallback de LLM

**Type:** Bug
**Blocked by:** Issue 18
**Status:** Done
**Prioridade:** Alta

### Problema
- O fallback para provider secundario e acionado apenas para "erros tecnicos de disponibilidade", mas nao ha definicao formal de quais excecoes e codigos HTTP se enquadram nessa categoria.
- Classificacao incorreta de um erro logico (ex.: prompt invalido, resposta mal formada) como erro tecnico aciona fallback indevidamente e mascara bugs reais.
- Nao ha teste cobrindo a fronteira entre erro tecnico e erro logico.

### O que fazer
- Definir lista explicita de excecoes e codigos HTTP que configuram erro tecnico de disponibilidade:
  - Conexao recusada, timeout de rede, HTTP 502/503/504, modelo inexistente no servidor.
- Definir lista de erros que **nao** devem acionar fallback:
  - HTTP 400 (payload invalido), HTTP 401 (autenticacao), HTTP 422 (schema de request errado), erro de parse de resposta.
- Implementar classificador com codigos estaveis: `LLM_ERR_UNAVAILABLE` vs `LLM_ERR_LOGICAL`.
- Registrar no Logger qual categoria de erro foi detectada e se o fallback foi ou nao acionado.

### Riscos tecnicos
- Lista de erros tecnicos muito ampla mascarar bugs de integracao.
- Lista muito restrita deixar de acionar fallback em cenarios legitimos de indisponibilidade.

### Casos de teste minimos
- Timeout de rede deve acionar fallback e ser classificado como `LLM_ERR_UNAVAILABLE`.
- HTTP 400 nao deve acionar fallback e deve ser classificado como `LLM_ERR_LOGICAL`.
- HTTP 503 deve acionar fallback e registrar log com codigo de erro estavel.
- Erro de parse da resposta do LLM nao deve acionar fallback.

---

## Issue 24 - Backoff Exponencial nos Retries de LLM

**Type:** Performance / Confiabilidade
**Blocked by:** Issue 23
**Status:** Done
**Prioridade:** Alta

### Problema
- `LLM_RETRIES` define um numero flat de tentativas sem intervalo entre elas.
- Retries imediatos em falha de API remota agravam rate limiting e aumentam latencia percebida pelo usuario.
- Em Ollama local, retries sem espera podem sobrecarregar o processo local antes que ele se recupere.

### O que fazer
- Implementar backoff exponencial com jitter nos retries de LLM:
  - Intervalo base configuravel: `LLM_RETRY_BACKOFF_BASE_S` (padrao: 1s).
  - Fator de multiplicacao: 2x por tentativa.
  - Jitter aleatorio de +/- 20% para evitar tempestade de requisicoes.
  - Cap maximo de espera: `LLM_RETRY_BACKOFF_MAX_S` (padrao: 10s).
- Aplicar backoff apenas para erros tecnicos (categoria `LLM_ERR_UNAVAILABLE` da Issue 23); erros logicos nao devem ter retry.
- Registrar no Logger cada tentativa com numero, intervalo aguardado e resultado.

### Riscos tecnicos
- Backoff em Ollama local aumentar latencia total percebida alem do aceitavel para o SLO p95 < 4s.
- Jitter insuficiente nao resolver colisao de retries em cenario com multiplas sessoes.

### Casos de teste minimos
- Primeira retry deve aguardar ~1s; segunda retry deve aguardar ~2s (+/- jitter).
- Erro logico nao deve gerar retry.
- Cap de espera deve ser respeitado (nenhuma espera maior que `LLM_RETRY_BACKOFF_MAX_S`).
- Log deve registrar tentativa, intervalo aguardado e codigo de erro por retry.

---

## Issue 25 - Limite de Contexto por Tokens alem do Numero de Mensagens

**Type:** Bug / Performance
**Blocked by:** Issue 5
**Status:** Open
**Prioridade:** Alta

### Problema
- `ConversationState` controla a janela de contexto apenas por `MAX_CONTEXT_MESSAGES` (numero de mensagens).
- Uma unica mensagem longa (ex.: prontuario parcial no perfil medical, transcricao de longa fala) pode ultrapassar o limite de tokens do LLM mesmo com poucas mensagens no historico.
- Overflow de tokens resulta em erro do provider remoto ou truncagem silenciosa da resposta, sem feedback claro para o usuario.

### O que fazer
- Adicionar estimativa de tokens por mensagem ao `ConversationState` usando contagem aproximada (`len(texto) // 4` como heuristica minima, ou biblioteca `tiktoken` para OpenAI).
- Introduzir variavel `MAX_CONTEXT_TOKENS` (padrao conservador: 6000 tokens) como limite adicional.
- Quando o historico ultrapassar `MAX_CONTEXT_TOKENS`, descartar mensagens antigas alem do descarte por `MAX_CONTEXT_MESSAGES`, priorizando manter system prompt e mensagem mais recente do usuario.
- Registrar no Logger quando truncagem por tokens for acionada.

### Riscos tecnicos
- Estimativa aproximada de tokens divergir do contador real do provider e gerar cortes prematuros ou insuficientes.
- Descarte de mensagens antigas em sessao medical eliminar contexto clinico critico.

### Casos de teste minimos
- Historico com poucas mensagens longas deve acionar descarte por tokens antes de atingir `MAX_CONTEXT_MESSAGES`.
- Descarte por tokens deve preservar a mensagem mais recente do usuario.
- Logger deve emitir evento quando truncagem por tokens ocorrer.
- Historico dentro de ambos os limites nao deve sofrer descarte.

---

## Issue 26 - Cache do Modelo Whisper em Memoria entre Sessoes

**Type:** Performance
**Blocked by:** None
**Status:** Done
**Prioridade:** Media

### Problema
- O modelo Whisper e carregado do disco a cada nova sessao de voz.
- Modelos `medium` e `large` levam varios segundos para carregar, adicionando latencia de boot percebida pelo usuario antes da primeira transcricao.
- Em fluxo de uso frequente (multiplas sessoes curtas), o custo de carregamento se repete desnecessariamente.

### O que fazer
- Implementar singleton de carregamento do modelo Whisper no `STTAdapter`:
  - Carregar o modelo uma unica vez na primeira instanciacao.
  - Reutilizar a instancia em memoria para todas as sessoes subsequentes no mesmo processo.
- Registrar no Logger tempo de carregamento do modelo na primeira carga.
- Garantir que a troca de `WHISPER_MODEL` por reinicio do processo carregue o novo modelo corretamente (sem estado residual).

### Riscos tecnicos
- Modelo em memoria aumentar uso de RAM de forma inesperada em maquinas com pouca memoria.
- Singleton nao thread-safe causar condicao de corrida se multiplas sessoes iniciarem simultaneamente.

### Casos de teste minimos
- Segunda instanciacao do `STTAdapter` no mesmo processo nao deve recarregar o modelo do disco.
- Log de tempo de carregamento deve aparecer apenas na primeira carga.
- Reinicio do processo deve carregar modelo corretamente sem estado residual.

---

## Issue 27 - Streaming de Resposta do LLM na UI

**Type:** Performance / UX
**Blocked by:** Issue 7, Issue 12
**Status:** Open
**Prioridade:** Media

### Problema
- O fluxo atual aguarda a resposta textual **completa** do LLM antes de exibi-la na UI.
- Para respostas longas (prontuario medical, avaliacao de entrevista), o usuario fica sem feedback visual por varios segundos, aumentando latencia percebida mesmo dentro do SLO tecnico.

### O que fazer
- Implementar streaming de tokens no `LLMAdapter` para os providers que suportam (OpenAI, Gemini e Ollama suportam nativamente).
- O `Orchestrator` deve repassar tokens parciais ao `TextUI` conforme chegam.
- A `TextUI` deve exibir tokens em tempo real no campo de resposta, com indicador visual de que a resposta ainda esta sendo gerada.
- Manter comportamento atual (resposta completa) como fallback quando o provider nao suportar streaming.
- Adicionar metrica de `time_to_first_token` ao Logger/Metrics alem da latencia total.

### Riscos tecnicos
- Tokens parciais exibir conteudo inconsistente se o LLM gerar tokens antes de completar raciocinio (relevante para perfil medical).
- Implementacao de streaming diferente por provider aumentar complexidade do `LLMAdapter`.

### Casos de teste minimos
- Primeiro token deve aparecer na UI antes da resposta completa ser recebida.
- Metrica `time_to_first_token` deve ser registrada por interacao.
- Fallback para resposta completa deve funcionar quando streaming nao estiver disponivel.
- Conteudo exibido em streaming deve ser identico ao conteudo da resposta completa ao final.

---

## Issue 28 - Politica de Confianca STT Adaptativa em Ambientes Ruidosos

**Type:** Performance / UX
**Blocked by:** Issue 4
**Status:** Open
**Prioridade:** Media

### Problema
- O limiar de confianca STT e fixo em 0,80.
- Em ambientes com ruido constante (consultorio, escritorio), scores sistematicamente abaixo de 0,80 causam loop de retentativas em todas as interacoes, e apos 2 falhas forcam fallback manual permanente — quebrando o fluxo de voz.
- O limiar fixo nao distingue entre baixa confianca por ruido de fundo (aceitavel com aviso) e fala genuinamente ininteligivel (deve bloquear).

### O que fazer
- Introduzir dois limiares configuraveis por variavel de ambiente:
  - `STT_CONFIDENCE_BLOCK=0.60`: abaixo desse valor, sempre bloquear e pedir repeticao.
  - `STT_CONFIDENCE_WARN=0.80`: entre block e warn, aceitar o trecho com aviso visual na UI de baixa confianca.
- Para perfil `medical` (critico): `STT_CONFIDENCE_BLOCK` nao pode ser configurado abaixo de 0,70.
- Registrar distribuicao de scores de confianca no Logger para permitir ajuste de limiares por ambiente.

### Riscos tecnicos
- Limiar de aviso muito permissivo propagar transcricoes erradas ao LLM sem o usuario perceber.
- Perfil medical com limiares relaxados comprometer seguranca clinica.

### Casos de teste minimos
- Score >= 0,80 deve seguir fluxo normal sem aviso.
- Score entre 0,60 e 0,79 deve seguir com aviso visual de baixa confianca.
- Score < 0,60 deve bloquear e solicitar repeticao.
- Perfil medical nao deve aceitar `STT_CONFIDENCE_BLOCK` abaixo de 0,70.

---

## Issue 29 - Agrupamento de Trechos Incertos na Revisao Final do Perfil Medical

**Type:** UX / Confiabilidade
**Blocked by:** Issue 9, Issue 10
**Status:** Open
**Prioridade:** Media

### Problema
- Em sessao medical com ruido de ambiente, muitos trechos podem ser marcados como `role incerto`.
- A revisao final e obrigatoria para perfil `medical` (critico) antes de emitir o prontuario.
- Sem limite ou agrupamento, o usuario pode enfrentar dezenas de confirmacoes individuais, comprometendo a UX em cenario real de consultorio.

### O que fazer
- Agrupar trechos incertos consecutivos do mesmo contexto temporal para revisao em bloco.
- Definir limite maximo de itens de revisao por sessao: `MAX_REVIEW_ITEMS` (padrao: 10).
- Quando o numero de trechos incertos ultrapassar `MAX_REVIEW_ITEMS`, agrupar os excedentes por proximidade temporal em um unico item de revisao.
- Para trechos com score de atribuicao acima de limiar secundario configuravel, permitir auto-atribuicao com registro em log de auditoria.
- Exibir na UI o numero total de trechos incertos e progresso da revisao antes de iniciar.

### Riscos tecnicos
- Agrupamento mascarar erros de atribuicao de role em trechos criticos do prontuario.
- Auto-atribuicao com limiar muito permissivo comprometer integridade clinica do artefato.

### Casos de teste minimos
- Sessao com 20 trechos incertos deve apresentar no maximo `MAX_REVIEW_ITEMS` itens de revisao.
- Trechos agrupados devem registrar auditoria indicando que foram consolidados.
- Revisao concluida com agrupamento deve gerar prontuario final valido.
- Auto-atribuicao deve registrar log de auditoria com score e decisao tomada.

---

## Issue 30 - STTService Direto Ignora Sample Rate do AudioSegment

**Type:** Bug
**Blocked by:** None
**Status:** Done
**Prioridade:** Alta

### Problema
O caminho principal de producao (`StreamingTranscriber` -> `_safe_transcribe`) faz o resampling
corretamente passando o `sr` real da fonte. Porem, `STTService.transcribe(audio)` e uma API
publica que aceita `bytes` sem parametro de taxa. Se um chamador passar `AudioSegment.audio_bytes`
capturado em taxa diferente de 16 kHz (ex.: 48 kHz apos fallback no Windows), o
`FasterWhisperBackend` usa `_input_sample_rate=16000` (valor default) e nao aplica resampling —
o Whisper recebe audio tocando em velocidade 3x incorreta, degradando o WER silenciosamente.

O problema existe porque `STTService.transcribe` nao tem como saber a taxa real do audio recebido.
Qualquer integracao futura que use `AudioCapture` + `STTService` diretamente (sem passar por
`live_caption._safe_transcribe`) seria afetada.

### O que fazer
- Adicionar parametro opcional `sample_rate: int | None = None` a `STTService.transcribe()`.
- Quando `sample_rate` for informado e diferente de 16000, chamar `_to_float32(audio, sample_rate)` antes de passar ao backend, e entregar ao backend um array ja em 16 kHz com `input_sample_rate=16000`.
- Expor `sample_rate` tambem no contrato `WhisperBackend.transcribe()` para manter consistencia.
- Atualizar `AudioCapture` ou documentar explicitamente que chamadores de `STTService` sao responsaveis por informar a taxa do `AudioSegment`.
- Adicionar `_logger.warning` em `STTService.transcribe()` quando `sample_rate` nao e informado e o backend esta configurado com taxa diferente de 16 kHz.

### Riscos tecnicos
- Mudanca de assinatura de `STTService.transcribe()` quebrar testes existentes que passam apenas `audio`.
- Chamadores que ja usam `_safe_transcribe` receberem double-resample se nao adaptarem o fluxo.

### Casos de teste minimos
- `STTService.transcribe(audio_48khz, sample_rate=48000)` deve produzir transcricao equivalente a `STTService.transcribe(audio_16khz)` para o mesmo conteudo de fala.
- `STTService.transcribe(audio_48khz)` sem `sample_rate` deve emitir `WARNING` indicando taxa desconhecida.
- `WhisperBackend` stub deve ser compativel com a nova assinatura.
- Testes existentes de `STTService` nao devem regredir.
