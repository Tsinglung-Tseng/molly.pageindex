const PAGEINDEX_DIR = __dirname;

module.exports = {
  apps: [
    {
      name        : 'pageindex-web',
      script      : `${PAGEINDEX_DIR}/start_web.sh`,
      interpreter : '/bin/bash',
      autorestart : true,
      watch       : false,
      max_memory_restart: '512M',
      env: {
        PYTHONUNBUFFERED: '1',
      },
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
    },
  ],
};
