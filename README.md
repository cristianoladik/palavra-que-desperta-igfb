# Template de publicação Meta — Palavra Que Desperta

Template isolado para um futuro repositório GitHub. Ele nasce bloqueado, com
filas vazias, sem mídia, IDs, tokens, histórico ou vínculo remoto. O coordenador
único publica Stories e Reels no Instagram e Facebook e só depois avalia a
limpeza compartilhada da Release.

## Bloqueios que vêm ativos

`politica_publicacao.json` é a fonte versionada de verdade. Enquanto qualquer
item abaixo estiver pendente, o primeiro GET da Meta nem acontece:

- `publicacao_habilitada` precisa mudar de `false` para `true`;
- `autoridade_unica_confirmada` precisa mudar de `false` para `true`, somente
  depois de confirmar que não existe outro robô publicando na mesma conta;
- `data_inicio_rampa` precisa receber uma data `AAAA-MM-DD` aprovada;
- `repositorio_esperado` precisa receber exatamente `dono/repositorio`.
- `github_visibilidade` precisa ser `publico`, a visibilidade real informada pelo
  workflow precisa ser `public`, e `exposicao_midias_futuras_confirmada` precisa
  ser `true` após consentimento explícito.

A ativação real ainda exige simultaneamente as variáveis de repositório
`PQD_AUTOMACAO_ATIVA=true`, `PQD_PUBLICACAO_CONFIRMADA=true` e
`PQD_PERSISTENCIA_GITHUB=true`. As duas primeiras também aparecem no `if` do
job, de modo que uma agenda desativada não aloca runner. Por isso, as duas
travas do `if` precisam ser Repository Variables (não apenas variáveis do
Environment, que só ficam disponíveis depois que o job inicia).

Use o Environment `palavra-que-desperta-publicacao-meta` e somente Secrets
exclusivos deste projeto:

- `PQD_IG_ACCESS_TOKEN`
- `PQD_IG_BUSINESS_ID`
- `PQD_FB_PAGE_ACCESS_TOKEN`
- `PQD_FB_PAGE_ID`
- `PQD_GITHUB_CONTENTS_TOKEN`

O token GitHub precisa ler e atualizar as duas filas via Contents API e excluir
assets da Release. Nunca copie credenciais, IDs, filas ou Releases de outra
marca.

Antes de qualquer mutação, o coordenador confere a política e as duas filas.
Depois exige as quatro credenciais Meta e faz somente dois GETs: valida que o
IG configurado tem username `palavraquedesperta_br` e que a Page configurada
está ligada exatamente a essa conta. Divergência bloqueia tudo.

## Agenda, rampa e teto real

O workflow faz polling a cada 15 minutos em `America/Sao_Paulo`; GitHub Actions
não garante disparo no minuto exato. A política aceita somente:

1. semana 1: 09:00 (um Reel/dia);
2. semana 2: 09:00 e 21:00 (dois/dia);
3. semana 3: 05:00, 09:00 e 21:00 (três/dia);
4. semana 4: 05:00, 09:00, 13:00 e 21:00 (quatro/dia);
5. semana 5 em diante: 05:00, 09:00, 13:00, 17:00 e 21:00 (cinco/dia).

Slots fora da semana, duplicidades e quantidade acima de 1/2/3/4/5 bloqueiam a
fila inteira. Há ainda um teto de execução **real por rede e por dia BRT**:
publicações já registradas em `publicado_em` consomem o saldo da semana atual.
Assim, um backlog nunca aquece a conta com dez Reels no mesmo dia. A ordem da
fila é preservada.

Reels compartilhados entre as duas redes precisam declarar duração entre 3 e
90 segundos; esses limites também fazem parte da política versionada.

Stories usam somente 09:00. Pode existir no máximo um pacote-fonte agendado por
data e iniciar no máximo um pacote-fonte no dia real BRT, inclusive ao consumir
backlog. O mesmo pacote pode continuar; um segundo pacote não pode começar.
Stories têm prioridade no coordenador das 09h. Uma falha neles não impede a
modalidade Reels, mas qualquer falha bloqueia a limpeza naquela execução.

O `workflow_dispatch` não recebe data nem horário. Execução manual consome
somente itens já vencidos; as funções também rejeitam seleção futura.

## Legenda e Stories

- Reels no Instagram e Facebook: exatamente `siga @palavraquedesperta_br`.
- Stories: nenhuma legenda textual enviada pela API.
- Cada parte de Story precisa ter duração maior que zero e no máximo 59 s.
- O corte em partes iguais e o limite de 59 s também são invariantes da
  política compartilhada com o sincronizador local; a tolerância de medição é
  de 0,25 s nas duas pontas.
- O pacote completo é validado por SHA-256 e tamanho antes da primeira chamada
  mutável. A cota de publicação do Instagram é consultada antes do envio.

## Estado durável e antirrepetição

Imediatamente antes da primeira mutação de cada rede, o estado
`publicacao_iniciada` é persistido localmente e na branch `main` pela GitHub
Contents API. Cada container, upload, confirmação e conclusão gera outro
checkpoint. O checkout usa `persist-credentials: false`; não há `git push` nem
credencial salva na configuração Git.

A atualização compara o conteúdo remoto com a versão esperada e recusa
sobrescrever alteração concorrente. Segredos conhecidos são redigidos de
exceções, resumos e JSON antes da gravação.

Se uma chamada de publicação falhar ou uma execução encontrar estado
intermediário, a rede muda para `revisao_manual`. Container, vídeo ou upload
parcial nunca é reenviado automaticamente. Uma rede com `status=publicado` e
`id` também nunca é repetida.

## Filas e limpeza

As filas versionadas começam vazias:

- `fila/fila-reels.json`
- `fila/fila-stories.json`

Cabeçalhos, esquema, canal e fuso são obrigatórios. Cada mídia precisa apontar
exatamente para `repositorio_esperado`, `release_tag` e o nome de asset presentes
na política; uma URL HTTPS de outro repositório ou domínio é recusada antes do
primeiro acesso à Meta.

A limpeza sempre carrega **as duas filas juntas**. Um nome, SHA-256 ou tamanho
divergente, ou qualquer referência pendente em qualquer modalidade, bloqueia a
exclusão. Antes e depois de excluir um asset, ambas as filas recebem checkpoint.
Se a exclusão ficar ambígua, o asset não é presumido como removido.

Este template não cria repositório, Release nem envia mídia. O abastecedor local
deve obter consentimento para a exposição por URL HTTPS antes de alimentar as
filas. Mídia nunca entra no Git nem no disco C. No runner Linux, o cache usa
`${runner.temp}`; no Windows, `PQD_MEDIA_CACHE_DIR` precisa resolver para um
descendente de:

`G:\Meu Drive\PROJETO SEGUNDO CÉREBRO Cristiano Ladik\Projeto - Palavra Que Desperta - Drive`

## Supply chain e testes

Checkout e setup do Python estão pinados por SHA. Todas as dependências e
transitivas estão pinadas com hashes; o workflow usa `--require-hashes` e
`--only-binary=:all:`. Os testes usam somente clientes falsos:

```powershell
python -m unittest discover -s tests -v
```

Antes de qualquer ativação ainda faltam o repositório exclusivo, a data inicial,
a confirmação de autoridade única, as credenciais, o consentimento de exposição
da mídia e um teste real explicitamente autorizado.
