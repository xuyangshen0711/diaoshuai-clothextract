"""A bounded image queue. Each ready batch gets a turn; same records stay ordered."""
from collections import OrderedDict, deque
from threading import Condition, Thread


class ImageQueue:
    def __init__(self, concurrency=2):
        self.condition = Condition()
        self.batches = OrderedDict()
        self.busy_keys = set()
        self.closing = False
        self.workers = [Thread(target=self._work, name=f"image-ai-{i}", daemon=True)
                        for i in range(concurrency)]
        for worker in self.workers:
            worker.start()

    def submit(self, key, items, process, finish, completion):
        with self.condition:
            self.batches[key] = dict(items=deque(items), process=process, finish=finish,
                                     completion=completion, running=0)
            # A newly ready batch should not wait behind the old batch's next image.
            self.batches.move_to_end(key, last=False)
            self.condition.notify_all()

    def _take(self):
        for key, batch in self.batches.items():
            if not batch['items']:
                continue
            item, resource = batch['items'][0]
            if resource in self.busy_keys:
                continue
            batch['items'].popleft()
            batch['running'] += 1
            self.busy_keys.add(resource)
            self.batches.move_to_end(key)
            return key, batch, item, resource
        return None

    def _work(self):
        while True:
            with self.condition:
                work = self._take()
                while work is None:
                    if self.closing and not self.batches:
                        return
                    self.condition.wait()
                    work = self._take()
            key, batch, item, resource = work
            failure = None
            try:
                stop = batch['process'](item)
            except Exception as exc:
                stop, failure = True, exc
            with self.condition:
                self.busy_keys.remove(resource)
                batch['running'] -= 1
                if stop:
                    batch['items'].clear()
                if failure:
                    batch['failure'] = failure
                finished = not batch['items'] and not batch['running']
                if finished:
                    del self.batches[key]
                self.condition.notify_all()
            if finished:
                # Run outside the condition: finalization can itself enqueue a retry.
                try:
                    batch['finish'](batch.get('failure'))
                    batch['completion'].set_result(None)
                except Exception as exc:
                    batch['completion'].set_exception(exc)

    def shutdown(self):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        for worker in self.workers:
            worker.join()
