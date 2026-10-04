# wallet-tracker

Telegram-бот: уведомления о переводах USDT/USDC на отслеживаемых адресах в Ethereum (ERC20), BSC (BEP20), TRON (TRC20).

## Настройка
Все параметры в `.env` (chmod 600): `TG_BOT_TOKEN`, `TG_ADMIN_ID`, `POLL_INTERVAL`, `MIN_AMOUNT`, `ETH_RPC`, `BSC_RPC`, `TRONGRID_URL`, `TRONGRID_API_KEY`, `DB_PATH`.

## Управление (только TG_ADMIN_ID)
Инлайн-меню по `/start`: добавить адрес (для 0x… выбор сети: ETH / BSC / обе), список с пагинацией,
карточка адреса (метка, удаление с подтверждением, ссылка на обозреватель), настройки и фильтры.
Быстро: `/add <адрес> [метка]`, `/list`, или просто отправить адрес сообщением.
Уведомления приходят с кнопками «Транзакция» и «Мои адреса».

Тесты: `selftest.py` (живые сети), `uitest.py` (меню офлайн).

## Фильтр скама
- токены не из белого списка контрактов (поддельные USDT/USDC) игнорируются;
- переводы на 0;
- входящие меньше `MIN_AMOUNT`;
- address poisoning: контрагент совпадает по первым и последним 4 символам с уже известным контрагентом или с вашим адресом.

## Запуск
```
uv venv .venv && uv pip install --python .venv/bin/python httpx
.venv/bin/python selftest.py      # проверка на живых сетях, в Telegram не пишет
systemctl --user enable --now wallet-tracker
journalctl --user -u wallet-tracker -f
```

## Источники данных
Публичные бесплатные: publicnode (eth_getLogs, лимит ~100 блоков назад), TronGrid (без ключа ~1 запрос/с).
После простоя дольше ~20 мин часть истории EVM может быть пропущена.
