# Расхождение git и деплой точного SHA на Vercel

## Переиспользуемый паттерн инцидента

Релизный коммит создан на фича-ветке, пока `origin/main` ушёл вперёд через merge-коммит.
`git push origin main` отклонён. После fetch история показала: фича-ветка и `main` связаны,
но разошлись. Чистый cherry-pick на `main` дал конфликты modify/delete, потому что результат
merge разрешил новые утилиты миграции как удалённые.

Безопасный исход:

1. Прерви cherry-pick; не разрешай вслепую.
2. Сравни `origin/main..feature-tip` по путям и осмотри merge-родителей.
3. Признай, что источник продакшена остался фича-веткой.
4. Fast-forward этой ветки: `git push origin HEAD:<проверенная-ветка>`.
5. Проверь равенство локального SHA и `git ls-remote`.
6. Задеплой detached чистый worktree на точном SHA.
7. Проверь статус Vercel, alias, `/api/health` и публичный маршрут.

## Дерево решений после отклонения пуша

```text
пуш отклонён
├─ ошибка связности
│  └─ ретрай; проверь удалённый SHA после успеха
└─ non-fast-forward
   ├─ fetch + осмотр счётчиков HEAD...remote и графа
   ├─ цель — текущая фича-ветка
   │  └─ пуш HEAD в эту проверенную ветку
   └─ цель — main
      ├─ чистая связь, нет семантических конфликтов
      │  └─ rebase/cherry-pick в чистом worktree, тесты, fast-forward пуш
      └─ modify/delete или неожиданный конфликт дерева
         └─ прерви; сравни деревья и разрешение merge до изменения main
```

## Рецепт деплоя точного SHA на Vercel

```bash
SHA=$(git rev-parse HEAD)
DEPLOY_DIR="D:/project-deploy-${SHA:0:8}"
git worktree add --detach "$DEPLOY_DIR" "$SHA"
```

Создай `$DEPLOY_DIR/.vercel/project.json` с существующими `projectId`, `orgId`,
`projectName`, затем:

```bash
cd "$DEPLOY_DIR"
npx vercel deploy --prod --yes
npx vercel inspect <deployment-url>
```

Проверь публичные эндпоинты независимо:

```bash
curl -sS -L https://example.vercel.app/api/health
curl -sS -L -o NUL -w '%{http_code}\n' https://example.vercel.app/
```

Уборка только после выхода процесса деплоя:

```bash
git worktree remove --force "$DEPLOY_DIR"
```

## Почему это важно

- Грязный чекаут может тихо добавить несвязанные файлы в загрузку CLI провайдера.
- Merge-коммит доказывает предков, но не то, что каждый файл фича-ветки пережил разрешение
  конфликта.
- Успешная команда деплоя не доказывает, что публичный alias и маршруты работают.
- Точное равенство локального/удалённого SHA плюс detached worktree дают проверяемый
  артефакт релиза.
