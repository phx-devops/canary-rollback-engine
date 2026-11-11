// canary-proxy: the ingress for the canary rollout.
//
//   - Splits traffic between named backends with smooth weighted round robin, so a 90/10 weight
//     pair gives an exact, evenly interleaved 90/10 split.
//   - Weights change live over a unix admin socket. Both weights are swapped under one lock, so
//     there is never an instant with no eligible backend and no request is dropped.
//   - Counts, per backend, every request it sent and every failure: a 5xx answer, a refused or reset
//     connection, or no response headers within the upstream timeout. It never retries a failed
//     request on another backend, so a failing canary can't be hidden behind the stable version.
//   - Starts from the weights given on the command line (100 % app-blue in the golden solution),
//     so a restarted proxy always comes back serving blue.
//
// Admin protocol: one JSON object per line on the unix socket.
//
//	{"cmd":"stats"}                                     -> snapshot
//	{"cmd":"set","weights":{"app-blue":90,"app-green":10}} -> snapshot, or {"error": "..."}
//
// A snapshot is {"instance","pid","weights":{name:int},"backends":{name:{"requests","failures"}}}.
// Counters are cumulative for this process; "instance" changes on every start so a scraper can
// detect a restart and re-baseline instead of computing negative deltas.
package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type backend struct {
	name     string
	rp       *httputil.ReverseProxy
	weight   int // guarded by proxy.mu
	current  int // smooth-WRR state, guarded by proxy.mu
	requests atomic.Uint64
	failures atomic.Uint64
}

type proxy struct {
	mu       sync.Mutex
	backends []*backend
	byName   map[string]*backend
	instance string
}

type counters struct {
	Requests uint64 `json:"requests"`
	Failures uint64 `json:"failures"`
}

type snapshot struct {
	Instance string              `json:"instance"`
	Pid      int                 `json:"pid"`
	Weights  map[string]int      `json:"weights"`
	Backends map[string]counters `json:"backends"`
}

func newProxy(targets []string, upstreamTimeout time.Duration) (*proxy, error) {
	p := &proxy{byName: map[string]*backend{}}
	id := make([]byte, 8)
	if _, err := rand.Read(id); err != nil {
		return nil, err
	}
	p.instance = hex.EncodeToString(id)
	for _, t := range targets {
		name, raw, ok := strings.Cut(t, "=")
		if !ok || name == "" {
			return nil, fmt.Errorf("backend %q: want name=url", t)
		}
		u, err := url.Parse(raw)
		if err != nil || u.Host == "" {
			return nil, fmt.Errorf("backend %q: bad url", t)
		}
		if _, dup := p.byName[name]; dup {
			return nil, fmt.Errorf("backend %q listed twice", name)
		}
		b := &backend{name: name}
		b.rp = &httputil.ReverseProxy{
			Rewrite: func(pr *httputil.ProxyRequest) { pr.SetURL(u); pr.Out.Host = pr.In.Host },
			Transport: &http.Transport{
				DialContext:           (&net.Dialer{Timeout: time.Second}).DialContext,
				ResponseHeaderTimeout: upstreamTimeout,
				// One upstream connection per request: a backend that dies can't keep answering
				// on a pooled connection, and every request is attributed on its own.
				DisableKeepAlives: true,
			},
			ModifyResponse: func(resp *http.Response) error {
				b.requests.Add(1)
				if resp.StatusCode >= 500 {
					b.failures.Add(1)
				}
				return nil
			},
			ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
				if errors.Is(r.Context().Err(), context.Canceled) {
					return // the client went away: not the backend's failure
				}
				b.requests.Add(1)
				b.failures.Add(1)
				status := http.StatusBadGateway
				var ne net.Error
				if errors.As(err, &ne) && ne.Timeout() {
					status = http.StatusGatewayTimeout
				}
				w.WriteHeader(status)
			},
			ErrorLog: log.New(os.Stderr, "upstream "+name+": ", log.LstdFlags),
		}
		p.backends = append(p.backends, b)
		p.byName[name] = b
	}
	if len(p.backends) == 0 {
		return nil, errors.New("no backends")
	}
	return p, nil
}

// pick implements smooth weighted round robin (as in nginx): exact proportions, evenly spread.
func (p *proxy) pick() *backend {
	p.mu.Lock()
	defer p.mu.Unlock()
	total := 0
	var best *backend
	for _, b := range p.backends {
		if b.weight <= 0 {
			continue
		}
		b.current += b.weight
		total += b.weight
		if best == nil || b.current > best.current {
			best = b
		}
	}
	if best != nil {
		best.current -= total
	}
	return best
}

func (p *proxy) setWeights(w map[string]int) error {
	sum := 0
	for name, v := range w {
		if _, ok := p.byName[name]; !ok {
			return fmt.Errorf("unknown backend %q", name)
		}
		if v < 0 || v > 1000 {
			return fmt.Errorf("weight for %q out of range: %d", name, v)
		}
		sum += v
	}
	if len(w) != len(p.backends) {
		return fmt.Errorf("give a weight for every backend (%d)", len(p.backends))
	}
	if sum == 0 {
		return errors.New("at least one weight must be positive")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, b := range p.backends {
		b.weight, b.current = w[b.name], 0
	}
	return nil
}

func (p *proxy) snapshot() snapshot {
	s := snapshot{Instance: p.instance, Pid: os.Getpid(), Weights: map[string]int{}, Backends: map[string]counters{}}
	p.mu.Lock()
	for _, b := range p.backends {
		s.Weights[b.name] = b.weight
	}
	p.mu.Unlock()
	for _, b := range p.backends {
		s.Backends[b.name] = counters{Requests: b.requests.Load(), Failures: b.failures.Load()}
	}
	return s
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b := p.pick()
	if b == nil {
		http.Error(w, "no backend", http.StatusServiceUnavailable)
		return
	}
	b.rp.ServeHTTP(w, r)
}

func (p *proxy) handleAdmin(c net.Conn) {
	defer c.Close()
	sc := bufio.NewScanner(c)
	enc := json.NewEncoder(c)
	for sc.Scan() {
		var req struct {
			Cmd     string         `json:"cmd"`
			Weights map[string]int `json:"weights"`
		}
		if err := json.Unmarshal(sc.Bytes(), &req); err != nil {
			enc.Encode(map[string]string{"error": "bad json: " + err.Error()})
			continue
		}
		switch req.Cmd {
		case "stats":
			enc.Encode(p.snapshot())
		case "set":
			if err := p.setWeights(req.Weights); err != nil {
				enc.Encode(map[string]string{"error": err.Error()})
			} else {
				enc.Encode(p.snapshot())
			}
		default:
			enc.Encode(map[string]string{"error": "unknown cmd " + strconv.Quote(req.Cmd)})
		}
	}
}

func listenAdmin(path string) (net.Listener, error) {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	l, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	return l, os.Chmod(path, 0o600)
}

func parseWeights(s string) (map[string]int, error) {
	w := map[string]int{}
	for _, kv := range strings.Split(s, ",") {
		k, v, ok := strings.Cut(kv, "=")
		n, err := strconv.Atoi(v)
		if !ok || err != nil {
			return nil, fmt.Errorf("weights: bad %q", kv)
		}
		w[k] = n
	}
	return w, nil
}

type multi []string

func (m *multi) String() string     { return strings.Join(*m, ",") }
func (m *multi) Set(v string) error { *m = append(*m, v); return nil }

func writePidfile(path string) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(strconv.Itoa(os.Getpid())+"\n"), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func main() {
	var backends multi
	listen := flag.String("listen", "127.0.0.1:8080", "client listen address")
	flag.Var(&backends, "backend", "name=url (repeat)")
	weights := flag.String("weights", "", "initial weights, e.g. app-blue=100,app-green=0")
	admin := flag.String("admin", "/run/canary/proxy.sock", "admin unix socket")
	pidfile := flag.String("pidfile", "/run/canary/proxy.pid", "pid file, written once serving")
	timeout := flag.Duration("upstream-timeout", 5*time.Second, "max wait for a backend's response headers")
	flag.Parse()

	p, err := newProxy(backends, *timeout)
	if err != nil {
		log.Fatal(err)
	}
	w, err := parseWeights(*weights)
	if err == nil {
		err = p.setWeights(w)
	}
	if err != nil {
		log.Fatal(err)
	}
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	al, err := listenAdmin(*admin)
	if err != nil {
		log.Fatal(err)
	}
	go func() {
		for {
			c, err := al.Accept()
			if err != nil {
				return
			}
			go p.handleAdmin(c)
		}
	}()
	srv := &http.Server{Handler: p, ReadHeaderTimeout: 10 * time.Second, ErrorLog: log.New(os.Stderr, "http: ", log.LstdFlags)}
	if err := writePidfile(*pidfile); err != nil {
		log.Fatal(err)
	}
	names := make([]string, 0, len(w))
	for k := range w {
		names = append(names, fmt.Sprintf("%s=%d", k, w[k]))
	}
	sort.Strings(names)
	log.Printf("canary-proxy %s serving %s, weights %s", p.instance, *listen, strings.Join(names, ","))

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-sig
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
		os.Remove(*admin)
		os.Remove(*pidfile)
		os.Exit(0)
	}()
	if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
