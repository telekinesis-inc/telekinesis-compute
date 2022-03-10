import os
import importlib
import time
import json
import asyncio
import telekinesis as tk
import docker
from functools import partial

from telekinesis_data import FileSync

def prepare_python_files(path, dependencies):
    dockerbase = importlib.resources.read_text(__package__, f"Dockerfile_python")
    deps_pip_names = [d if isinstance(d, str) else d[0] for d in dependencies]
    deps_import_names = [d if isinstance(d, str) else d[1] for d in dependencies]

    dockerfile = dockerbase.replace('{{PKG_DEPENDENCIES}}', '\n'.join('RUN pip install '+ d for d in deps_pip_names))

    with open(os.path.join(path, 'Dockerfile'), 'w') as file_out:
        file_out.write(dockerfile)

    scriptbase = importlib.resources.read_text(__package__, "script_base.py")
    script = '\n'.join(['import '+ d.replace('-', '_') for d in deps_import_names] + [scriptbase])


    with open(os.path.join(path, 'script.py'), 'w') as file_out:
        file_out.write(script)

def prepare_pytorch_files(path, dependencies):
    dockerbase = importlib.resources.read_text(__package__, f"Dockerfile_pytorch")
    deps_pip_names = [d if isinstance(d, str) else d[0] for d in dependencies]
    deps_import_names = [d if isinstance(d, str) else d[1] for d in dependencies]

    dockerfile = dockerbase.replace('{{PKG_DEPENDENCIES}}', '\n'.join('RUN pip install '+ d for d in deps_pip_names))

    with open(os.path.join(path, 'Dockerfile'), 'w') as file_out:
        file_out.write(dockerfile)

    scriptbase = importlib.resources.read_text(__package__, "script_base.py")
    script = '\n'.join(['import '+ d.replace('-', '_') for d in set(deps_import_names).union(['torch'])] + [scriptbase])


    with open(os.path.join(path, 'script.py'), 'w') as file_out:
        file_out.write(script)

def prepare_js_files(path, dependencies):
    dockerbase = importlib.resources.read_text(__package__, "Dockerfile_js")
    dockerfile = dockerbase.replace('{{PKG_DEPENDENCIES}}', '\n'.join('RUN npm install '+ d for d in dependencies))

    with open(os.path.join(path, 'Dockerfile'), 'w') as file_out:
        file_out.write(dockerfile)

    scriptbase = importlib.resources.read_text(__package__, "script_base.js")
    script = '\n'.join([f'require({d});' for d in dependencies] + [scriptbase])

    with open(os.path.join(path, 'script.js'), 'w') as file_out:
        file_out.write(script)

class AppManager:
    def __init__(self, session, path, url=None):
        self.running = {}
        self.ready = {}
        self.client = docker.from_env()
        self.url = url or list(session.connections)[0].url
        self._session = session
        self.path = os.path.abspath(path)
        self.tasks = {'delayed_provisioning': {}, 'stop_callback': {}, 'check_running_loop': asyncio.create_task(self.loop_check_running())}

    async def build_image(self, pkg_dependencies, base):
        tag = '-'.join(['tk', base, *[d if isinstance(d, str) else d[0] for d in pkg_dependencies]])
        if base == 'python':
            prepare_python_files(self.path, pkg_dependencies)
        elif base == 'pytorch':
            prepare_pytorch_files(self.path, pkg_dependencies)
        elif base == 'js':
            prepare_js_files(self.path, pkg_dependencies)
        else:
            raise NotImplementedError("Only implemented bases are 'python', 'pytorch' and 'js'")


        cmd = f'docker build -t {tag} {self.path}'

        build = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE
        )
        await build.stdout.read()
        # await self.client.images.build(path_dockerfile='./docker_telekinesis_python/', tag=tag)

    async def start_container(self, pkg_dependencies, base, cpus, memory, gpu):
        tag = '-'.join(['tk', base, *[d if isinstance(d, str) else d[0] for d in pkg_dependencies]])


        def create_callbackable():
            e = asyncio.Event()
            data = {}

            async def awaiter():
                await e.wait()
                print('called awaiter')
                return data['data']

            return (awaiter, lambda *x: data.update({'data': x}) or e.set())

        awaiter, callback = create_callbackable()

        client_session = tk.Session()
        client_pubkey = client_session.session_key.public_serial()
        
        data_path = os.path.join(self.path, client_pubkey[:32].replace('/','-'))
        os.mkdir(data_path)

        route = await tk.Telekinesis(callback, self._session)._delegate(client_pubkey)

        _key_dump = json.dumps(client_session.session_key._private_serial().decode().strip('\n'))
        environment = [
            f"TELEKINESIS_URL='{self.url}'",
            f"TELEKINESIS_POD_NAME='id={client_session.session_key.public_serial()[:6]}, base={base}, cpus={cpus:.2f}, memory={int(memory)}, gpu={gpu}'",
            f"TELEKINESIS_ROUTE_STR='{json.dumps(route.to_dict())}'",
            "TELEKINESIS_PRIVATE_KEY_STR='"+_key_dump+"'"
        ]

        cmd = " ".join([
            f"docker run -e {' -e '.join(environment)} -d --rm --network=host -v {data_path}:/usr/src/app/data/",
            f"{'--gpus all --ipc=host' if gpu else ''} --cpus={cpus:.2f} --memory='{int(memory)}m'",
            f"-l telekinesis-compute {tag}"
        ])

        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE
        )

        container_id = (await process.stdout.read()).decode().replace('\n','')

        update_callbacks, pod = await awaiter()
        # container = self.client.containers.get(container_id)

        pod_wrapper = PodWrapper(container_id, pod, update_callbacks)
        return pod_wrapper

    async def clear_containers(self):
        [c.stop(timeout=0) for c in self.client.containers.list(all=True, filters={'label': 'telekinesis-compute'})]
        [c.remove() for c in self.client.containers.list(all=True, filters={'label': 'telekinesis-compute'})]

        return self.client.images.prune()

    async def get_pod(
        self, pkg_dependencies, account_id, base='python', cpus=1.0, memory=2000, gpu=False, autostop_timeout=None, bind_data=None, stop_callback=None, 
        provision=False, upgrade=False
    ):
        tag = '-'.join(['tk', base, *[d if isinstance(d, str) else d[0] for d in pkg_dependencies]])
        if not self.ready.get(tag):
            print('awaiting provisioning')
            await self.provision(1, pkg_dependencies, base, cpus, memory, gpu, upgrade)
        pod_wrapper = self.ready[tag].pop()

        self.running[account_id] = {**self.running.get(account_id, {}), pod_wrapper.id: pod_wrapper}

        # if stop_callback or autostop_timeout is not None:
        await pod_wrapper.update_params(partial(self.stop, account_id, pod_wrapper.id, stop_callback), autostop_timeout)
        pod_wrapper.reset_timeout()
        
        if bind_data:
            data_path = os.path.join(self.path, pod_wrapper.id[:32].replace('/','-'))
            support_path = data_path +'_support'
            os.mkdir(support_path)

            pod_wrapper.filesync = FileSync(bind_data, data_path, support_path)

        if provision:
            t = time.time()
            async def delayed_provisioning(t):
                await asyncio.sleep(1)
                await self.provision(1, base, pkg_dependencies, base, cpus, memory, gpu, upgrade)
                self.tasks['delayed_provisioning'].pop(t)
            self.tasks['delayed_provisioning'][t] = asyncio.create_task(delayed_provisioning(t))

        return pod_wrapper.pod

    async def provision(self, number, pkg_dependencies, base, cpus, memory, gpu, upgrade):
        print('provisioning', number)
        tag = '-'.join(['tk', base, *[d if isinstance(d, str) else d[0] for d in pkg_dependencies]])
        if not tag in self.ready:
            self.ready[tag] = []

        if upgrade or not self.client.images.list(name=tag):
            await self.build_image(pkg_dependencies, base)

        self.ready[tag].extend(
            await asyncio.gather(*[self.start_container(pkg_dependencies, base, cpus, memory, gpu) for _ in range(number)])
        )

    async def stop(self, account_id, pod_id, callback=None):
        p = self.running.get(account_id, {}).pop(pod_id)
        if p and callback:
            self.tasks['stop_callback'][time.time()] = asyncio.create_task(callback(pod_id)._execute())
        elif callback:
            print(pod_id, 'not found')

    async def check_running(self):
        running_containers = (await (await asyncio.create_subprocess_shell(
            'docker container ls -q --no-trunc', 
            stdout=asyncio.subprocess.PIPE)
        ).stdout.read()).decode().strip('\n').split('\n')

        for account_pods in self.running.values():
            for pod_wrapper in account_pods.values():
                if pod_wrapper.container_id not in running_containers:
                    print('container stopped', pod_wrapper.container_id)
                    await pod_wrapper.stop(False)
    
    async def loop_check_running(self):
        while True:
            # try:
                await asyncio.sleep(15)
                await self.check_running()
            # except BaseException:
                # pass

class PodWrapper:
    def __init__(self, container_id, pod, update_callbacks):
        self.container_id = container_id
        self.pod_update_callbacks = update_callbacks
        self.pod = pod
        self.id = pod._target.session[0]
        self.stop_callback = None
        self.autostop_timeout = None
        self.autostop_time = 0
        self.autostop_task = None
        self.filesync = None

    def reset_timeout(self):
        print('>>>> keep alive')
        if self.autostop_timeout is not None:
            self.autostop_time = time.time() + self.autostop_timeout
            if self.autostop_task is None:
                self.autostop_task = asyncio.create_task(self.autostop(self.autostop_timeout))

    async def autostop(self, delay):
        await asyncio.sleep(delay)
        if self.autostop_time and self.autostop_time < time.time():
            p = await asyncio.create_subprocess_shell(
                'docker stats --no-stream --format "{{.CPUPerc}}" '+self.container_id[:10],
                stdout=asyncio.subprocess.PIPE)
            cpu_utilization = float((await p.stdout.read()).decode().strip('%\n'))
            if cpu_utilization < 1: # 1%
                await self.stop()
            else:
                print('extending cpu_utilization', cpu_utilization)
                self.autostop_task = asyncio.create_task(self.autostop(max(2, self.autostop_timeout)))
        else:
            print('extending', self.autostop_time - time.time())
            self.autostop_task = asyncio.create_task(self.autostop(max(2, self.autostop_time-time.time())))
         
    async def update_params(self, stop_callback, autostop_timeout):
        self.stop_callback = stop_callback
        self.autostop_timeout = autostop_timeout

        await self.pod_update_callbacks(partial(self.stop, False), self.reset_timeout or 0)
    
    async def stop(self, stop_pod=True):
        if self.stop_callback:
            await self.stop_callback()
        if self.filesync and self.filesync.task:
            self.filesync.task.cancel()
        
        if stop_pod:
            try:
                await self.pod.stop()._timeout(5)
            except asyncio.TimeoutError:
                pass
        

