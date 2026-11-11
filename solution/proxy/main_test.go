package main

import (
	"bufio"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func app(name string, status int, delay time.Duration) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(delay)
		w.Header().Set("X-App", name)
		w.WriteHeader(status)
		io.WriteString(w, `{"app":"`+name+`"}`)
	}))
}

func mustProxy(t *testing.T, timeout time.Duration, targets ...string) *proxy {
	t.Helper()
	p, err := newProxy(targets, timeout)
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func get(t *testing.T, url string) (int, string) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	io.ReadAll(resp.Body)
	return resp.StatusCode, resp.Header.Get("X-App")
}

func TestSmoothWRRIsExactAndInterleaved(t *testing.T) {
	p := mustProxy(t, time.Second, "app-blue=http://127.0.0.1:1", "app-green=http://127.0.0.1:2")
	for _, tc := range []struct{ blue, green, wantGreen, maxRun int }{{90, 10, 10, 9}, {50, 50, 50, 1}, {0, 100, 100, 100}, {100, 0, 0, 100}} {
		if err := p.setWeights(map[string]int{"app-blue": tc.blue, "app-green": tc.green}); err != nil {
			t.Fatal(err)
		}
		green, run, maxRun := 0, 0, 0
		var last string
		for i := 0; i < 100; i++ {
			b := p.pick()
			if b.name == "app-green" {
				green++
			}
			if b.name == last {
				run++
			} else {
				run = 1
			}
			last = b.name
			if run > maxRun {
				maxRun = run
			}
		}
		if green != tc.wantGreen || maxRun > tc.maxRun {
			t.Errorf("%d/%d: green=%d want %d, longest run %d want <= %d", tc.blue, tc.green, green, tc.wantGreen, maxRun, tc.maxRun)
		}
	}
}

func TestSetWeightsValidation(t *testing.T) {
	p := mustProxy(t, time.Second, "app-blue=http://127.0.0.1:1", "app-green=http://127.0.0.1:2")
	for _, w := range []map[string]int{
		{"app-blue": 0, "app-green": 0}, {"app-blue": -1, "app-green": 10}, {"app-blue": 100}, {"app-blue": 50, "nope": 50},
	} {
		if p.setWeights(w) == nil {
			t.Errorf("accepted %v", w)
		}
	}
}

func TestFailuresAttributedPerBackendWithoutRetry(t *testing.T) {
	blue, green := app("app-blue", 500, 0), app("app-green", 200, 0) // blue failing must not count against green
	defer blue.Close()
	defer green.Close()
	dead := httptest.NewServer(http.NotFoundHandler())
	deadURL := dead.URL
	dead.Close() // connection refused from now on

	p := mustProxy(t, time.Second, "app-blue="+blue.URL, "app-green="+green.URL, "app-dead="+deadURL)
	p.setWeights(map[string]int{"app-blue": 1, "app-green": 1, "app-dead": 1})
	front := httptest.NewServer(p)
	defer front.Close()
	codes := map[int]int{}
	for i := 0; i < 30; i++ {
		c, _ := get(t, front.URL)
		codes[c]++
	}
	s := p.snapshot().Backends
	if s["app-blue"] != (counters{10, 10}) || s["app-green"] != (counters{10, 0}) || s["app-dead"] != (counters{10, 10}) {
		t.Errorf("counters %+v", s)
	}
	if codes[502] != 10 || codes[500] != 10 || codes[200] != 10 {
		t.Errorf("a refused request was retried elsewhere or mis-coded: %v", codes)
	}
}

func TestUpstreamTimeoutIsAFailure(t *testing.T) {
	slow := app("app-green", 200, 500*time.Millisecond)
	defer slow.Close()
	p := mustProxy(t, 100*time.Millisecond, "app-green="+slow.URL)
	p.setWeights(map[string]int{"app-green": 1})
	front := httptest.NewServer(p)
	defer front.Close()
	start := time.Now()
	if c, _ := get(t, front.URL); c != http.StatusGatewayTimeout {
		t.Errorf("status %d, want 504", c)
	}
	if time.Since(start) > 400*time.Millisecond {
		t.Errorf("timeout not enforced: %v", time.Since(start))
	}
	if s := p.snapshot().Backends["app-green"]; s != (counters{1, 1}) {
		t.Errorf("counters %+v", s)
	}
}

func TestResponsePassesThrough(t *testing.T) {
	green := app("app-green", 200, 0)
	defer green.Close()
	p := mustProxy(t, time.Second, "app-green="+green.URL)
	p.setWeights(map[string]int{"app-green": 1})
	front := httptest.NewServer(p)
	defer front.Close()
	resp, err := http.Get(front.URL + "/some/path")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.Header.Get("X-App") != "app-green" || string(body) != `{"app":"app-green"}` {
		t.Errorf("header %q body %q", resp.Header.Get("X-App"), body)
	}
}

func TestLiveWeightChangesDropNothing(t *testing.T) {
	blue, green := app("app-blue", 200, 0), app("app-green", 200, 0)
	defer blue.Close()
	defer green.Close()
	p := mustProxy(t, time.Second, "app-blue="+blue.URL, "app-green="+green.URL)
	p.setWeights(map[string]int{"app-blue": 100, "app-green": 0})
	front := httptest.NewServer(p)
	defer front.Close()
	stop := make(chan struct{})
	var wg sync.WaitGroup
	var mu sync.Mutex
	bad := 0
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				resp, err := http.Get(front.URL)
				if err != nil || resp.StatusCode != 200 {
					mu.Lock()
					bad++
					mu.Unlock()
				}
				if err == nil {
					io.Copy(io.Discard, resp.Body)
					resp.Body.Close()
				}
			}
		}()
	}
	for _, w := range [][2]int{{90, 10}, {50, 50}, {0, 100}, {100, 0}, {90, 10}, {0, 100}} {
		time.Sleep(50 * time.Millisecond)
		if err := p.setWeights(map[string]int{"app-blue": w[0], "app-green": w[1]}); err != nil {
			t.Fatal(err)
		}
	}
	close(stop)
	wg.Wait()
	if bad != 0 {
		t.Errorf("%d requests failed across weight changes", bad)
	}
}

func TestAdminProtocol(t *testing.T) {
	p := mustProxy(t, time.Second, "app-blue=http://127.0.0.1:1", "app-green=http://127.0.0.1:2")
	p.setWeights(map[string]int{"app-blue": 100, "app-green": 0})
	path := filepath.Join(t.TempDir(), "admin.sock")
	l, err := listenAdmin(path)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			go p.handleAdmin(c)
		}
	}()
	c, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	r := bufio.NewReader(c)
	ask := func(s string) map[string]any {
		c.Write([]byte(s + "\n"))
		line, err := r.ReadBytes('\n')
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]any
		json.Unmarshal(line, &m)
		return m
	}
	if m := ask(`{"cmd":"set","weights":{"app-blue":90,"app-green":10}}`); m["weights"].(map[string]any)["app-green"].(float64) != 10 {
		t.Errorf("set: %v", m)
	}
	if m := ask(`{"cmd":"stats"}`); m["instance"] != p.instance || m["backends"] == nil {
		t.Errorf("stats: %v", m)
	}
	if m := ask(`{"cmd":"set","weights":{"app-blue":0,"app-green":0}}`); m["error"] == nil {
		t.Errorf("all-zero weights accepted: %v", m)
	}
	if m := ask(`not json`); m["error"] == nil {
		t.Errorf("bad json accepted: %v", m)
	}
}
