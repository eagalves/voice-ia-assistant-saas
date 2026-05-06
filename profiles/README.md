# Perfis de Assistente

Este diretorio contem perfis versionados para configurar comportamento do assistente sem alterar o nucleo da aplicacao.

## Selecao de Perfil

Use variaveis de ambiente para escolher o perfil:

- `ASSISTANT_PROFILE=medical`
- `ASSISTANT_PROFILE=clinical_pharmacist_interviewer`
- `ASSISTANT_PROFILE=clinical_nurse_interviewer`
- `ASSISTANT_PROFILE=general_interviewer`

No MVP, a troca de perfil deve ocorrer no inicio da sessao (sem troca em runtime).

## Perfis Disponiveis

### 1) medical
Arquivo: `medical.yaml`

Objetivo:
- Anamnese guiada com prontuario parcial/final, foco em seguranca.

Principais regras:
- Campos obrigatorios de anamnese.
- Politica de STT com limiar de confianca.
- Bloqueio de envio ao LLM abaixo do limiar e fallback para texto.
- Checagem obrigatoria de sinais de alerta.
- SLOs operacionais do MVP.

### 2) clinical_pharmacist_interviewer
Arquivo: `clinical_pharmacist_interviewer.yaml`

Objetivo:
- Entrevistar candidato para vaga de Farmaceutico Clinico.

Parametro configuravel:
- `SENIORITY_LEVEL=junior|pleno|senior`

Principais regras:
- Entrevista em etapas com uma pergunta por vez.
- Banco de perguntas focado em protocolo clinico, atendimento humanizado, saude digital e postura de aprendizado.
- Pesos de avaliacao por senioridade.
- Saida estruturada com recomendacao final.

### 3) general_interviewer
Arquivo: `general_interviewer.yaml`

Objetivo:
- Perfil base para entrevistas de emprego em diferentes dominios.

Parametro configuravel:
- `SENIORITY_LEVEL=junior|pleno|senior`

Principais regras:
- Fluxo de entrevista estruturado por etapas.
- Perguntas tecnicas e comportamentais reaproveitaveis.
- Pesos por senioridade e saida padronizada de decisao.

### 4) clinical_nurse_interviewer
Arquivo: `clinical_nurse_interviewer.yaml`

Objetivo:
- Entrevistar candidato para vaga de Enfermagem Clinica.

Parametro configuravel:
- `SENIORITY_LEVEL=junior|pleno|senior`

Principais regras:
- Entrevista em etapas com uma pergunta por vez.
- Banco de perguntas focado em protocolo assistencial, seguranca do paciente, atendimento humanizado e rotina digital.
- Pesos de avaliacao por senioridade.
- Saida estruturada com recomendacao final.

## Exemplo de Ambiente

```env
ASSISTANT_PROFILE=clinical_pharmacist_interviewer
SENIORITY_LEVEL=junior
LANG=pt-BR
```

## Convencoes

- Um arquivo por perfil.
- `profile.id` deve ser unico, estavel e em `snake_case`.
- Nome de arquivo deve seguir o `profile.id` em `snake_case`.
- O loader deve resolver perfis por `profile.id` (chave canonica), com validacao estrita.
- Falhas de validacao devem interromper inicializacao do perfil (fail-fast de configuracao).
- Toda mudanca relevante incrementa `profile.version`.
- Regras do dominio ficam no perfil; orquestracao fica no nucleo.

## Contrato Tecnico do Loader

### Entrada

- Variavel de ambiente `ASSISTANT_PROFILE` obrigatoria.
- Variavel de ambiente `SENIORITY_LEVEL` obrigatoria apenas para perfis de entrevista.

### Resolucao Canonica

- O loader deve abrir todos os arquivos `.yaml` do diretorio `profiles/`.
- Para cada arquivo, deve ler `profile.id` e montar um indice por id.
- A selecao final deve ser feita por igualdade exata entre `ASSISTANT_PROFILE` e `profile.id`.
- Nome de arquivo nao pode ser usado como chave de negocio.

### Ordem de Validacao (fail-fast)

1. Validar presenca de `ASSISTANT_PROFILE`.
2. Validar parse YAML do arquivo alvo.
3. Validar campos minimos: `profile.id`, `profile.version`, `runtime`.
4. Validar unicidade de `profile.id` no catalogo.
5. Validar compatibilidade de `SENIORITY_LEVEL` quando aplicavel.
6. Carregar perfil e iniciar sessao.

### Erros Esperados

- `PROFILE_ENV_MISSING`: `ASSISTANT_PROFILE` ausente.
- `PROFILE_NOT_FOUND`: id nao encontrado no catalogo.
- `PROFILE_YAML_INVALID`: YAML invalido.
- `PROFILE_SCHEMA_INVALID`: estrutura minima invalida.
- `PROFILE_ID_DUPLICATE`: ids duplicados no catalogo.
- `PROFILE_PARAM_INVALID`: parametro de runtime invalido (ex.: seniority fora do enum).

### Observabilidade Minima

- Logar `profile.id` e `profile.version` na inicializacao.
- Logar erro com codigo estavel e contexto minimo (sem dados sensiveis).

### Validacao por Schema

- Schema base: `profiles/schema/profile.schema.json`.
- O boot deve validar o YAML carregado contra este schema antes de iniciar sessao.
- O schema cobre campos minimos; regras de dominio podem ter validacao complementar.
- Exemplo valido minimo: `profiles/schema/valid_profile_minimal.yaml`.
- Resultado esperado: validacao aprovada e inicializacao do perfil.
- Exemplo invalido para teste: `profiles/schema/invalid_profile_example.yaml`.
- Resultado esperado: falha de validacao com erro de schema (fail-fast de configuracao).

### Checklist de Teste do Loader

- [ ] Cenario 1 (valido): `ASSISTANT_PROFILE=valid_profile_minimal` com `profiles/schema/valid_profile_minimal.yaml` deve inicializar com sucesso.
- [ ] Cenario 2 (schema invalido): usar `profiles/schema/invalid_profile_example.yaml` deve falhar com erro de schema.
- [ ] Cenario 3 (id inexistente): `ASSISTANT_PROFILE` nao presente no catalogo deve falhar com `PROFILE_NOT_FOUND`.
