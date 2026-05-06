# PRD - Assistente de Voz Local com Perfis Configuráveis (MVP)

## Problem Statement

O usuário precisa de um assistente de voz local que capture fala pelo microfone, transcreva com STT local, envie o contexto a um LLM remoto via API key e devolva respostas em texto. O comportamento do assistente deve ser configurável por domínio, sem reescrever o núcleo: o mesmo sistema deve funcionar como assistente médico (anamnese e prontuário), entrevistador clínico (farmacêutico ou enfermeiro) ou entrevistador geral, apenas trocando um perfil via variável de ambiente. O MVP deve suportar conversas com um ou mais participantes e registrar papéis de fala por participante; no perfil médico isso cobre consulta (profissional + paciente) e pós-consulta (profissional sozinho refinando prontuário). Hoje não existe nenhuma base de código; o projeto começa do zero.

---

## Solution

Um aplicativo Python local com 8 módulos coesos e de baixo acoplamento: AudioInput, STTAdapter, ConversationState, LLMAdapter, Orchestrator, TextUI, Config e Logger. O comportamento de domínio é encapsulado em arquivos YAML de perfil, carregados no boot por um loader com validação estrita. O Orchestrator conhece apenas contratos abstratos; as implementações concretas (Whisper local, OpenAI GPT-4o) são injetadas por configuração. O fluxo registra quem falou por mensagem usando papéis de participante definidos por perfil.

---

## User Stories

1. Como usuário, quero iniciar e parar a escuta pelo microfone pela interface, para controlar quando o assistente está ativo.
2. Como usuário, quero que o assistente transcreva minha fala em pt-BR automaticamente, para não precisar digitar.
3. Como usuário, quero que o assistente me avise quando não entendeu minha fala, para poder repetir ou digitar manualmente.
4. Como usuário, quero um campo de entrada de texto de fallback quando o STT falhar 2 vezes consecutivas, para não perder dados importantes.
5. Como usuário, quero confirmar o texto digitado no fallback antes de enviar ao LLM, para evitar erros por digitação.
6. Como usuário, quero que o assistente mantenha histórico curto da conversa durante a sessão, para as respostas levarem o contexto em conta.
7. Como usuário, quero ver a transcrição e a resposta do assistente na tela, para acompanhar o fluxo da conversa.
8. Como usuário, quero ver o status atual do sistema na tela (ouvindo, transcrevendo, processando, erro), para entender o que está acontecendo.
9. Como usuário, quero configurar o perfil do assistente via variável de ambiente, para alternar entre modos (médico, entrevistador, geral) sem tocar no código.
10. Como usuário, quero que o assistente médico me faça perguntas de anamnese uma por vez, para me sentir em uma consulta guiada.
11. Como paciente simulado, quero que o assistente cubra os 8 campos obrigatórios de anamnese antes de gerar o prontuário, para ter um registro completo.
12. Como paciente simulado, quero receber um prontuário parcial com aviso das pendências quando não terminar todos os campos, para saber o que falta.
13. Como paciente simulado, quero que o assistente inclua seção de sinais de alerta no prontuário quando relatar sintomas críticos, para receber orientação de segurança.
14. Como candidato entrevistado, quero que o entrevistador faça perguntas uma por vez adaptadas ao nível configurado (junior, pleno, senior), para ter uma entrevista realista.
15. Como candidato entrevistado, quero receber uma avaliação estruturada ao final da entrevista, com pontos fortes, riscos e recomendação.
16. Como operador, quero configurar o nível de senioridade da entrevista via variável de ambiente, para testar diferentes perfis de vaga.
17. Como operador, quero que chaves de API e configurações sensíveis venham exclusivamente de variáveis de ambiente, para não expor credenciais no código.
18. Como operador, quero que o sistema falhe com mensagem clara na inicialização quando a configuração for inválida, para não descobrir erros em runtime.
19. Como operador, quero ver latência de STT e LLM registrada por interação, para monitorar o desempenho e identificar regressões.
20. Como operador, quero que erros por módulo sejam logados com código estável e contexto mínimo sem dados sensíveis, para facilitar debug.
21. Como operador, quero trocar o perfil de domínio sem alterar o Orchestrator ou qualquer módulo de núcleo, para escalar para novos casos de uso com baixo risco.
22. Como desenvolvedor, quero que os contratos de STTAdapter e LLMAdapter sejam estáveis e independentes de implementação, para trocar Whisper ou OpenAI por outro provider sem reescrever o fluxo.
23. Como médico, quero usar o assistente durante uma consulta presencial com paciente, para capturar e estruturar informações por papel de fala.
24. Como médico, quero continuar sozinho após a consulta para refinar o prontuário com o assistente, mantendo contexto da sessão.
25. Como sistema, quero registrar cada mensagem com role de participante, para preservar contexto correto no LLM em qualquer perfil.

---

## Implementation Decisions

### Stack
- Python como linguagem principal.
- Whisper (via `faster-whisper`) para STT local em pt-BR.
- OpenAI GPT-4o como provider LLM remoto.
- PyAudio ou `sounddevice` para captura de áudio.
- `python-dotenv` para carregamento de variáveis de ambiente.
- `pyyaml` + `jsonschema` para carregamento e validação de perfis.

### Módulos principais

**Config**
- Carrega variáveis de ambiente no boot.
- Expõe valores tipados para os demais módulos.
- Nenhuma key hardcoded no código.

**ProfileLoader**
- Lê todos os arquivos YAML de perfis no diretório `profiles/`.
- Constrói catálogo indexado por `profile.id`.
- Valida cada perfil contra o schema JSON base (`profiles/schema/profile.schema.json`).
- Resolve o perfil ativo por igualdade exata com `ASSISTANT_PROFILE`.
- Ordem de validação fail-fast: presença da variável, parse YAML, campos mínimos, unicidade de id, parâmetros de runtime.
- Erros com códigos estáveis: `PROFILE_ENV_MISSING`, `PROFILE_NOT_FOUND`, `PROFILE_YAML_INVALID`, `PROFILE_SCHEMA_INVALID`, `PROFILE_ID_DUPLICATE`, `PROFILE_PARAM_INVALID`.

**AudioInput**
- Captura áudio do microfone local.
- VAD simples para detectar início e fim de fala.
- Janela de áudio de 2 a 6 segundos por trecho.
- Deve anexar metadado de origem de fala por trecho (speaker_id/source_id) para o Orchestrator.
- Sem dependência dos módulos de IA.

**STTAdapter**
- Contrato: recebe trecho de áudio, devolve texto, idioma e score de confiança.
- Implementação `LocalSTT` via Whisper no MVP.
- Prioridade: precisão sobre latência.
- Limiar de confiança: 0,80. Abaixo desse valor, bloquear envio ao LLM e pedir repetição.
- Máximo de 2 repetições por trecho. Após falha: fallback para texto manual com confirmação curta obrigatória.
- Meta de latência STT: p95 ≤ 2,5s por trecho.

**ConversationState**
- Mantém histórico curto da sessão em memória (RAM).
- Janela configurável por `MAX_CONTEXT_MESSAGES`.
- Cada mensagem deve ser armazenada com `role` e conteúdo (ex.: profissional, cliente, candidato, assistente).
- Sem dependência de AudioInput, STT ou LLM.

**LLMAdapter**
- Contrato: recebe prompt atual e histórico, devolve resposta textual e metadados básicos (latência, tokens).
- Implementação `ApiLLM` via OpenAI GPT-4o no MVP.
- System prompt construído a partir das regras do perfil ativo.
- Política de falha: até 2 tentativas com timeout curto; em falha, exibir mensagem útil e manter sessão ativa.

**Orchestrator**
- Fluxo: receber áudio → STT → atualizar ConversationState → LLM → entregar resposta à UI → registrar métricas.
- Conhece apenas contratos abstratos; não referencia implementações concretas.
- Deve mapear `speaker_id/source_id` para `role` de participante antes de gravar no estado.
- Deve permitir sessão com múltiplos participantes e sessão individual; no perfil médico isso corresponde a `consultation` e `doctor_followup`.
- Para perfil `medical`: executar checagem obrigatória de sinais de alerta antes do prontuário final; se detectado, incluir seção de alerta e orientação de atendimento presencial/urgência.
- Para perfis de entrevista: controlar ciclo de no máximo 3 perguntas antes de emitir resumo parcial.

**Politica de atribuicao de participante (MVP)**
- Identificação automática em background do participante principal assistido (ex.: profissional), sem interromper o atendimento.
- Trechos com baixa confiança ficam marcados como `role` incerto durante a conversa.
- Revisão rápida no fechamento para confirmar trechos incertos antes de emitir artefato final.
- Revisão final é obrigatória para perfis críticos (ex.: medical) e opcional para perfis não críticos.
- No MVP, apenas o perfil `medical` é classificado como crítico.

**TextUI**
- Interface mínima: iniciar/parar, campo de transcrição, campo de resposta, campo de status.
- Status visíveis: ouvindo, transcrevendo, processando, erro.
- Expõe campo de entrada manual para fallback de STT.
- Comunica-se apenas com o Orchestrator.

**Logger/Metrics**
- Registra latência de STT e LLM por interação.
- Registra erros por módulo com código estável.
- Sem dados sensíveis nos logs.

### Contratos de Perfil

- Perfis em `profiles/`, um arquivo YAML por perfil, nome de arquivo igual ao `profile.id` em `snake_case`.
- `profile.id` é a chave canônica; nome de arquivo não pode ser usado como chave de negócio.
- Cada perfil define: `output_contract` (seções obrigatórias de saída), regras de domínio, guardrails e parâmetros de runtime.
- Perfis de entrevista aceitam parâmetro `SENIORITY_LEVEL=junior|pleno|senior`.
- Perfil médico aceita `MEDICAL_SESSION_MODE=consultation|doctor_followup`.

### Política de Falhas (fail-soft)
- Até 2 tentativas por etapa com timeout curto.
- STT: pedir repetição ao usuário.
- LLM: mostrar mensagem útil, manter sessão.
- Após 2 falhas de STT: fallback para texto manual com confirmação curta obrigatória antes de seguir.

---

## Testing Decisions

Um bom teste valida comportamento externo observável, não detalhes de implementação. Testar o contrato do módulo, não sua estrutura interna.

**ProfileLoader**
- Testar os 3 cenários do checklist: válido mínimo, schema inválido, id inexistente.
- Testar unicidade de id com catálogo duplicado.
- Testar parâmetro de runtime inválido (seniority fora do enum).

**STTAdapter**
- Testar transcricão correta acima do limiar de confiança (enviado ao LLM).
- Testar bloqueio abaixo do limiar (não envia ao LLM).
- Testar ciclo de até 2 repetições e ativação do fallback manual.

**Orchestrator**
- Testar fluxo ponta a ponta com mocks de STTAdapter e LLMAdapter.
- Testar checagem de sinais de alerta no perfil `medical`.
- Testar limite de 3 perguntas por ciclo nos perfis de entrevista.
- Testar mapeamento de `speaker_id/source_id` para `role` em sessão multi-participante.
- Testar marcação de `role` incerto e revisão final antes do artefato de saída.
- Testar obrigatoriedade de revisão em perfis críticos e comportamento opcional nos demais.

**SLOs (testes de aceite)**
- WER ≤ 10% em 50 frases fixas (30 em silêncio, 20 com ruído leve).
- Latência p95 ponta a ponta < 4s.
- Estabilidade: 30 minutos contínuos com taxa de erro técnico < 2%.

---

## Out of Scope

- TTS (resposta em voz).
- Integração nativa com Teams ou chamadas de vídeo.
- Suporte a múltiplos usuários simultâneos.
- Deploy em nuvem.
- Troca de perfil em runtime pela UI.
- Banco de dados ou persistência de sessão entre execuções.
- Validação médica formal por profissional de saúde.

---

## Further Notes

- A arquitetura está documentada em `arquitetura-assistente-voz-mvp.md`.
- Os perfis de domínio estão em `profiles/`, com guia de uso em `profiles/README.md`.
- O arquivo `.env.example` contém presets comentados para cada perfil.
- O schema de validação de perfil está em `profiles/schema/profile.schema.json`.
- O sistema é projetado para escalar para novos perfis e novos providers de STT/LLM sem alterar o núcleo.
