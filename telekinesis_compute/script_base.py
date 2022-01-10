import asyncio
import telekinesis as tk
import os
import json

class Instance:
    def __init__(self, lock):
        self.scopes = {}
        self._lock = lock

    async def execute(self, code, inputs=None, scope=None, print_callback=None):
        inputs = inputs or {}
        if scope:
            inputs.update(self.scopes.get(scope, {}))
        prefix = 'async def _wrapper(_new, _print_callback):\n'
        # TODO print_callback
        content = ('\n'+code).replace('\n', '\n    ')
        suffix = """
    for _var in dir():
        if _var[0] != '_':
            _new[_var] = eval(_var)"""
        exec(prefix+content+suffix, inputs)
        new_vars = {}
        await inputs['_wrapper'](new_vars)

        if scope:
            self.scopes[scope] = new_vars

        return new_vars
    def stop(self):
        self._lock.set()

async def main():
    print('starting')
    e = asyncio.Event()

    with open('../session_key.pem', 'w') as file:
        file.write(os.environ['PRIVATEKEY'].replace('\\', '\n'))

    entrypoint = await tk.Entrypoint(os.environ['URL'], '../session_key.pem')
    route = tk.Route(**json.loads(os.environ['ROUTE']))

    await tk.Telekinesis(route, entrypoint._session)(Instance(e))

    print('running')

    await e.wait()
    print('stopping')

asyncio.run(main())