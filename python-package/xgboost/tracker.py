"""
This script is a variant of dmlc-core/dmlc_tracker/tracker.py,
which is a specialized version for xgboost tasks.
"""

# pylint: disable=invalid-name, missing-docstring, too-many-arguments, too-many-locals
# pylint: disable=too-many-branches, too-many-statements, too-many-instance-attributes
import socket
import struct
import time
import logging
from threading import Thread
import argparse
import sys

from typing import Dict, List, Tuple, Union, Optional

_RingMap = Dict[int, Tuple[int, int]]
_TreeMap = Dict[int, List[int]]


class ExSocket:
    """
    Extension of socket to handle recv and send of special data
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def recvall(self, nbytes: int) -> bytes:
        res = []
        nread = 0
        while nread < nbytes:
            chunk = self.sock.recv(min(nbytes - nread, 1024))
            nread += len(chunk)
            res.append(chunk)
        return b''.join(res)

    def recvint(self) -> int:
        return struct.unpack('@i', self.recvall(4))[0]

    def sendint(self, n: int) -> None:
        self.sock.sendall(struct.pack('@i', n))

    def sendstr(self, s: str) -> None:
        self.sendint(len(s))
        self.sock.sendall(s.encode())

    def recvstr(self) -> str:
        slen = self.recvint()
        return self.recvall(slen).decode()


# magic number used to verify existence of data
kMagic = 0xff99


def get_some_ip(host: str) -> str:
    return socket.getaddrinfo(host, None)[0][4][0]


def get_family(addr: str) -> int:
    return socket.getaddrinfo(addr, None)[0][0]


class WorkerEntry:
    def __init__(self, sock: socket.socket, s_addr: Tuple[str, int]):
        worker = ExSocket(sock)
        self.sock = worker
        self.host = get_some_ip(s_addr[0])
        magic = worker.recvint()
        assert magic == kMagic, f"invalid magic number={magic} from {self.host}"
        worker.sendint(kMagic)
        self.rank = worker.recvint()
        self.world_size = worker.recvint()
        self.jobid = worker.recvstr()
        self.cmd = worker.recvstr()
        self.wait_accept = 0
        self.port: Optional[int] = None

    def decide_rank(self, job_map: Dict[str, int]) -> int:
        if self.rank >= 0:
            return self.rank
        if self.jobid != 'NULL' and self.jobid in job_map:
            return job_map[self.jobid]
        return -1

    def assign_rank(
        self,
        rank: int,
        wait_conn: Dict[int, "WorkerEntry"],
        tree_map: _TreeMap,
        parent_map: Dict[int, int],
        ring_map: _RingMap,
    ) -> List[int]:
        self.rank = rank
        nnset = set(tree_map[rank])
        rprev, rnext = ring_map[rank]
        self.sock.sendint(rank)
        # send parent rank
        self.sock.sendint(parent_map[rank])
        # send world size
        self.sock.sendint(len(tree_map))
        self.sock.sendint(len(nnset))
        # send the rprev and next link
        for r in nnset:
            self.sock.sendint(r)
        # send prev link
        if rprev not in (-1, rank):
            nnset.add(rprev)
            self.sock.sendint(rprev)
        else:
            self.sock.sendint(-1)
        # send next link
        if rnext not in (-1, rank):
            nnset.add(rnext)
            self.sock.sendint(rnext)
        else:
            self.sock.sendint(-1)
        while True:
            ngood = self.sock.recvint()
            goodset = set([])
            for _ in range(ngood):
                goodset.add(self.sock.recvint())
            assert goodset.issubset(nnset)
            badset = nnset - goodset
            conset = []
            for r in badset:
                if r in wait_conn:
                    conset.append(r)
            self.sock.sendint(len(conset))
            self.sock.sendint(len(badset) - len(conset))
            for r in conset:
                self.sock.sendstr(wait_conn[r].host)
                port = wait_conn[r].port
                assert port is not None
                self.sock.sendint(port)
                self.sock.sendint(r)
            nerr = self.sock.recvint()
            if nerr != 0:
                continue
            self.port = self.sock.recvint()
            rmset = []
            # all connection was successuly setup
            for r in conset:
                wait_conn[r].wait_accept -= 1
                if wait_conn[r].wait_accept == 0:
                    rmset.append(r)
            for r in rmset:
                wait_conn.pop(r, None)
            self.wait_accept = len(badset) - len(conset)
            return rmset


class RabitTracker:
    """
    tracker for rabit
    """

    def __init__(
        self, hostIP: str,
        n_workers: int,
        port: int = 9091,
        port_end: int = 9999,
        use_logger: bool = False,
    ) -> None:
        """A Python implementation of RABIT tracker.

        Parameters
        ..........
        use_logger:
            Use logging.info for tracker print command.  When set to False, Python print
            function is used instead.

        """
        sock = socket.socket(get_family(hostIP), socket.SOCK_STREAM)
        for _port in range(port, port_end):
            try:
                sock.bind((hostIP, _port))
                self.port = _port
                break
            except socket.error as e:
                if e.errno in [98, 48]:
                    continue
                raise
        sock.listen(256)
        self.sock = sock
        self.hostIP = hostIP
        self.thread: Optional[Thread] = None
        self.n_workers = n_workers
        self._use_logger = use_logger
        logging.info('start listen on %s:%d', hostIP, self.port)

    def __del__(self) -> None:
        if hasattr(self, "sock"):
            self.sock.close()

    @staticmethod
    def get_neighbor(rank: int, n_workers: int) -> List[int]:
        rank = rank + 1
        ret = []
        if rank > 1:
            ret.append(rank // 2 - 1)
        if rank * 2 - 1 < n_workers:
            ret.append(rank * 2 - 1)
        if rank * 2 < n_workers:
            ret.append(rank * 2)
        return ret

    def worker_envs(self) -> Dict[str, Union[str, int]]:
        """
        get environment variables for workers
        can be passed in as args or envs
        """
        return {'DMLC_TRACKER_URI': self.hostIP,
                'DMLC_TRACKER_PORT': self.port}

    def get_tree(self, n_workers: int) -> Tuple[_TreeMap, Dict[int, int]]:
        tree_map: _TreeMap = {}
        parent_map: Dict[int, int] = {}
        for r in range(n_workers):
            tree_map[r] = self.get_neighbor(r, n_workers)
            parent_map[r] = (r + 1) // 2 - 1
        return tree_map, parent_map

    def find_share_ring(
        self, tree_map: _TreeMap, parent_map: Dict[int, int], r: int
    ) -> List[int]:
        """
        get a ring structure that tends to share nodes with the tree
        return a list starting from r
        """
        nset = set(tree_map[r])
        cset = nset - set([parent_map[r]])
        if not cset:
            return [r]
        rlst = [r]
        cnt = 0
        for v in cset:
            vlst = self.find_share_ring(tree_map, parent_map, v)
            cnt += 1
            if cnt == len(cset):
                vlst.reverse()
            rlst += vlst
        return rlst

    def get_ring(self, tree_map: _TreeMap, parent_map: Dict[int, int]) -> _RingMap:
        """
        get a ring connection used to recover local data
        """
        assert parent_map[0] == -1
        rlst = self.find_share_ring(tree_map, parent_map, 0)
        assert len(rlst) == len(tree_map)
        ring_map: _RingMap = {}
        n_workers = len(tree_map)
        for r in range(n_workers):
            rprev = (r + n_workers - 1) % n_workers
            rnext = (r + 1) % n_workers
            ring_map[rlst[r]] = (rlst[rprev], rlst[rnext])
        return ring_map

    def get_link_map(self, n_workers: int) -> Tuple[_TreeMap, Dict[int, int], _RingMap]:
        """
        get the link map, this is a bit hacky, call for better algorithm
        to place similar nodes together
        """
        tree_map, parent_map = self.get_tree(n_workers)
        ring_map = self.get_ring(tree_map, parent_map)
        rmap = {0: 0}
        k = 0
        for i in range(n_workers - 1):
            k = ring_map[k][1]
            rmap[k] = i + 1

        ring_map_: _RingMap = {}
        tree_map_: _TreeMap = {}
        parent_map_: Dict[int, int] = {}
        for k, v in ring_map.items():
            ring_map_[rmap[k]] = (rmap[v[0]], rmap[v[1]])
        for k, tree_nodes in tree_map.items():
            tree_map_[rmap[k]] = [rmap[x] for x in tree_nodes]
        for k, parent in parent_map.items():
            if k != 0:
                parent_map_[rmap[k]] = rmap[parent]
            else:
                parent_map_[rmap[k]] = -1
        return tree_map_, parent_map_, ring_map_

    def accept_workers(self, n_workers: int) -> None:
        # set of nodes that finishes the job
        shutdown: Dict[int, WorkerEntry] = {}
        # set of nodes that is waiting for connections
        wait_conn: Dict[int, WorkerEntry] = {}
        # maps job id to rank
        job_map: Dict[str, int] = {}
        # list of workers that is pending to be assigned rank
        pending: List[WorkerEntry] = []
        # lazy initialize tree_map
        tree_map = None

        start_time = time.time()

        while len(shutdown) != n_workers:
            fd, s_addr = self.sock.accept()
            s = WorkerEntry(fd, s_addr)
            if s.cmd == 'print':
                msg = s.sock.recvstr()
                # On dask we use print to avoid setting global verbosity.
                if self._use_logger:
                    logging.info(msg.strip())
                else:
                    print(msg.strip(), flush=True)
                continue
            if s.cmd == 'shutdown':
                assert s.rank >= 0 and s.rank not in shutdown
                assert s.rank not in wait_conn
                shutdown[s.rank] = s
                logging.debug('Received %s signal from %d', s.cmd, s.rank)
                continue
            assert s.cmd in ("start", "recover")
            # lazily initialize the workers
            if tree_map is None:
                assert s.cmd == 'start'
                if s.world_size > 0:
                    n_workers = s.world_size
                tree_map, parent_map, ring_map = self.get_link_map(n_workers)
                # set of nodes that is pending for getting up
                todo_nodes = list(range(n_workers))
            else:
                assert s.world_size in (-1, n_workers)
            if s.cmd == 'recover':
                assert s.rank >= 0

            rank = s.decide_rank(job_map)
            # batch assignment of ranks
            if rank == -1:
                assert todo_nodes
                pending.append(s)
                if len(pending) == len(todo_nodes):
                    pending.sort(key=lambda x: x.host)
                    for s in pending:
                        rank = todo_nodes.pop(0)
                        if s.jobid != 'NULL':
                            job_map[s.jobid] = rank
                        s.assign_rank(rank, wait_conn, tree_map, parent_map, ring_map)
                        if s.wait_accept > 0:
                            wait_conn[rank] = s
                        logging.debug('Received %s signal from %s; assign rank %d',
                                      s.cmd, s.host, s.rank)
                if not todo_nodes:
                    logging.info('@tracker All of %d nodes getting started', n_workers)
            else:
                s.assign_rank(rank, wait_conn, tree_map, parent_map, ring_map)
                logging.debug('Received %s signal from %d', s.cmd, s.rank)
                if s.wait_accept > 0:
                    wait_conn[rank] = s
        logging.info('@tracker All nodes finishes job')
        end_time = time.time()
        logging.info(
            '@tracker %s secs between node start and job finish',
            str(end_time - start_time)
        )

    def start(self, n_workers: int) -> None:
        def run() -> None:
            self.accept_workers(n_workers)

        self.thread = Thread(target=run, args=(), daemon=True)
        self.thread.start()

    def join(self) -> None:
        while self.thread is not None and self.thread.is_alive():
            self.thread.join(100)

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


def get_host_ip(hostIP: Optional[str] = None) -> str:
    if hostIP is None or hostIP == 'auto':
        hostIP = 'ip'

    if hostIP == 'dns':
        hostIP = socket.getfqdn()
    elif hostIP == 'ip':
        from socket import gaierror
        try:
            hostIP = socket.gethostbyname(socket.getfqdn())
        except gaierror:
            logging.debug(
                'gethostbyname(socket.getfqdn()) failed... trying on hostname()'
            )
            hostIP = socket.gethostbyname(socket.gethostname())
        if hostIP.startswith("127."):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # doesn't have to be reachable
            s.connect(('10.255.255.255', 1))
            hostIP = s.getsockname()[0]

    assert hostIP is not None
    return hostIP


def start_rabit_tracker(args: argparse.Namespace) -> None:
    """Standalone function to start rabit tracker.

    Parameters
    ----------
    args: arguments to start the rabit tracker.
    """
    envs = {"DMLC_NUM_WORKER": args.num_workers, "DMLC_NUM_SERVER": args.num_servers}
    rabit = RabitTracker(
        hostIP=get_host_ip(args.host_ip), n_workers=args.num_workers, use_logger=True
    )
    envs.update(rabit.worker_envs())
    rabit.start(args.num_workers)
    sys.stdout.write("DMLC_TRACKER_ENV_START\n")
    # simply write configuration to stdout
    for k, v in envs.items():
        sys.stdout.write(f"{k}={v}\n")
    sys.stdout.write("DMLC_TRACKER_ENV_END\n")
    sys.stdout.flush()
    rabit.join()


def main() -> None:
    """Main function if tracker is executed in standalone mode."""
    parser = argparse.ArgumentParser(description='Rabit Tracker start.')
    parser.add_argument('--num-workers', required=True, type=int,
                        help='Number of worker process to be launched.')
    parser.add_argument(
        '--num-servers', default=0, type=int,
        help='Number of server process to be launched. Only used in PS jobs.'
    )
    parser.add_argument('--host-ip', default=None, type=str,
                        help=('Host IP addressed, this is only needed ' +
                              'if the host IP cannot be automatically guessed.'))
    parser.add_argument('--log-level', default='INFO', type=str,
                        choices=['INFO', 'DEBUG'],
                        help='Logging level of the logger.')
    args = parser.parse_args()

    fmt = '%(asctime)s %(levelname)s %(message)s'
    if args.log_level == 'INFO':
        level = logging.INFO
    elif args.log_level == 'DEBUG':
        level = logging.DEBUG
    else:
        raise RuntimeError(f"Unknown logging level {args.log_level}")

    logging.basicConfig(format=fmt, level=level)

    if args.num_servers == 0:
        start_rabit_tracker(args)
    else:
        raise RuntimeError("Do not yet support start ps tracker in standalone mode.")


if __name__ == "__main__":
    main()
