import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {connectionCommand,shellQuote} from './compute.js';
test('quote preserves literal shell metacharacters',()=>{
 const value="a'b $(echo should-not-run) `echo test`";
 assert.equal(execFileSync('bash',['-c','printf %s '+shellQuote(value)],{encoding:'utf8'}),value);
});
test('container command preserves environment values and uses one GPU',()=>{
 const pair={agent_image:'ghcr.io/owner/agent:commit',studio_url:'https://studio.example',pairing_code:"test'code"};
 const args=JSON.parse(execFileSync('bash',['-c','docker(){ python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@"; }; '+connectionCommand(pair)],{encoding:'utf8'}));
 assert.ok(args.includes('device=0'));assert.ok(args.includes('PAIRING_CODE='+pair.pairing_code));assert.equal(args.at(-1),pair.agent_image);
});
test('no guessed container image',()=>assert.equal(connectionCommand({}),''));
