# Copyright (c) 2016-2018, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.


import asyncio
import pylru

from electrumx.server.mempool import MemPool


class ChainState(object):
    '''Used as an interface by servers to request information about
    blocks, transaction history, UTXOs and the mempool.
    '''

    def __init__(self, env, tasks, notifications):
        self._env = env
        self._tasks = tasks
        self._daemon = env.coin.DAEMON(env)
        BlockProcessor = env.coin.BLOCK_PROCESSOR
        self._bp = BlockProcessor(env, tasks, self._daemon, notifications)
        self._mempool = MemPool(env.coin, tasks, self._daemon, self,
                                notifications)
        self._history_cache = pylru.lrucache(256)

        # External interface pass-throughs for session.py
        self.force_chain_reorg = self._bp.force_chain_reorg
        self.mempool_fee_histogram = self._mempool.get_fee_histogram
        self.mempool_get_utxos = self._mempool.get_utxos
        self.mempool_potential_spends = self._mempool.potential_spends
        self.mempool_transactions = self._mempool.transactions
        self.mempool_value = self._mempool.value
        self.tx_branch_and_root = self._bp.merkle.branch_and_root
        self.read_headers = self._bp.read_headers
        # Cache maintenance
        notifications.add_callback(self._notify)

    async def _notify(self, height, touched):
        # Invalidate our history cache for touched hashXs
        hc = self._history_cache
        for hashX in set(hc).intersection(touched):
            del hc[hashX]

    async def broadcast_transaction(self, raw_tx):
        return await self._daemon.sendrawtransaction([raw_tx])

    async def daemon_request(self, method, args=()):
        return await getattr(self._daemon, method)(*args)

    def db_height(self):
        return self._bp.db_height

    def get_info(self):
        '''Chain state info for LocalRPC and logs.'''
        return {
            'daemon': self._daemon.logged_url(),
            'daemon_height': self._daemon.cached_height(),
            'db_height': self.db_height(),
        }

    async def get_history(self, hashX):
        '''Get history asynchronously to reduce latency.'''
        def job():
            # History DoS limit.  Each element of history is about 99
            # bytes when encoded as JSON.  This limits resource usage
            # on bloated history requests, and uses a smaller divisor
            # so large requests are logged before refusing them.
            limit = self._env.max_send // 97
            return list(self._bp.get_history(hashX, limit=limit))

        hc = self._history_cache
        if hashX not in hc:
            hc[hashX] = await self._tasks.run_in_thread(job)
        return hc[hashX]

    async def get_utxos(self, hashX):
        '''Get UTXOs asynchronously to reduce latency.'''
        def job():
            return list(self._bp.get_utxos(hashX, limit=None))

        return await self._tasks.run_in_thread(job)

    def header_branch_and_root(self, length, height):
        return self._bp.header_mc.branch_and_root(length, height)

    def processing_new_block(self):
        '''Return True if we're processing a new block.'''
        return self._daemon.cached_height() > self.db_height()

    def raw_header(self, height):
        '''Return the binary header at the given height.'''
        header, n = self._bp.read_headers(height, 1)
        if n != 1:
            raise IndexError(f'height {height:,d} out of range')
        return header

    def set_daemon_url(self, daemon_url):
        self._daemon.set_urls(self._env.coin.daemon_urls(daemon_url))
        return self._daemon.logged_url()

    async def shutdown(self):
        '''Shut down the block processor to flush chain state to disk.'''
        await self._bp.shutdown()

    async def wait_for_mempool(self):
        await self._bp.catch_up_to_daemon()
        await self._mempool.start_and_wait_for_sync()