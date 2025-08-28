#!/bin/sh

LOG_DIR="/app/logs"
mkdir -p "$LOG_DIR"
LOG=$LOG_DIR"/frontend.log"
ERRS=$LOG_DIR"/front_errors.log"

echo $LOG
echo `date `" Started " > $LOG
ls -l /app/logs

cd /app/frontend

# Проверка и создание Vue-проекта, один раз
if [ ! -f /app/frontend/package.json ]; then
  echo "package.json отсутствует, ожидается интерактивная инициализация Vue-проекта" 
  sleep 300 
  ls -l /app/frontend
  if [ -f /app/frontend/package.json ]; then
     echo "Установка npm зависимостей" >>$LOG
     npm install --prefix /app/frontend >>$LOG 2>>$ERRS 
  else
     echo "FATAL: package.json not created..." >> $ERRS
     return
  fi
  if [ -f /app/App.vue ]; then
    cp /app/App.vue /app/frontend/src/App.vue >>$LOG 2>>$ERRS
  fi
fi


# Запуск dev-сервера
echo "Запуск npm run dev" >> $LOG
npm install
npm run dev --prefix /app/frontend -- --host 0.0.0.0 --port 8008 >>$LOG 2>> $ERRS
echo `date `" Finished " >> $LOG
