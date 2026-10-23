#!/usr/bin/env node
'use strict';

// npx frappe-setup-cli [command] [options]  ->  bundled install.sh -> Python installer
const { main } = require('../lib/launcher');

main().then(
  (code) => {
    process.exitCode = code;
  },
  (err) => {
    process.stderr.write(`x Unexpected launcher error: ${err && err.stack ? err.stack : err}\n`);
    process.exitCode = 1;
  },
);
