import collections
import operator
import sys
import traceback

import networkx as nx
from networkx.algorithms.shortest_paths.generic import shortest_path

from ..core.combinatorial import get_node_clusters, get_vhg_cdn_mapping
from ..pricing.generator import get_vmg_calculator, get_vcdn_calculator


class AbstractServiceTopo(object):
    def __init__(self, sla, vhg_count, vcdn_count, hint_node_mappings=None):

        mapped_start_nodes = sla.get_start_nodes()
        mapped_cdn_nodes = sla.get_cdn_nodes()
        self.sla_id = sla.id
        self.delay = sla.delay

        self.servicetopo, self.delay_paths, self.delay_routes = self.compute_service_topo(
            mapped_start_nodes=mapped_start_nodes, mapped_cdn_nodes=mapped_cdn_nodes, vhg_count=vhg_count,
            vcdn_count=vcdn_count,
            hint_node_mappings=hint_node_mappings, substrate=sla.substrate)

    def compute_service_topo(self, substrate, mapped_start_nodes, mapped_cdn_nodes, vhg_count, vcdn_count,hint_node_mappings=None):
        raise NotImplementedError("Must override methodB")

    def get_vhg(self):
        return self.get_nodes_by_type("VHG", self.servicetopo)

    def get_vcdn(self):
        return self.get_nodes_by_type("VCDN", self.servicetopo)

    def get_cdn(self):
        return self.get_nodes_by_type("CDN", self.servicetopo)

    def get_Starters(self):
        return [(s, self.servicetopo.node[s]["mapping"],self.servicetopo.node[s]["bandwidth"]) for s in self.get_nodes_by_type("S", self.servicetopo)]

    def get_nodes_by_type(self, type, graph):
        '''

        :param type: "VHG"
        :param graph:
        :return: ["VHG1","VHG2"]
        '''
        return [n[0] for n in graph.nodes(data=True) if n[1].get("type") == type]

    def getServiceNodes(self):
        for node in self.servicetopo.nodes(data=True):
            yield node[0], node[1].get("cpu", 0), node[1].get("bandwidth", 0)

    def getServiceCDNNodes(self):
        cdns = self.get_cdn()
        for node in self.servicetopo.nodes(data=True):
            if node[0] in cdns:
                yield node[0], node[1].get("cpu", 0)

    def dump_edges(self):
        '''
        :return: [(start , end , bandwidth)]
        '''
        res = []
        for start, ends in self.servicetopo.edge.items():
            for end in ends:
                edge = self.servicetopo[start][end]
                res.append((start, end, edge.get("bandwidth",0)))
        return res

    def getServiceCDNEdges(self):
        '''

        :return: start, end, edge["bandwidth"]
        '''
        for start, ends in self.servicetopo.edge.items():
            cdns = self.get_cdn()
            for end in [end for end in ends if end in cdns]:
                edge = self.servicetopo[start][end]
                yield start, end, edge.get("bandwidth",0)

    def getServiceEdges(self):
        '''

        :return: start, end, edge["bandwidth"]
        '''
        for start, ends in self.servicetopo.edge.items():
            for end in ends:
                edge = self.servicetopo[start][end]
                yield start, end, edge.get("bandwidth",0)

    def dump_delay_paths(self):
        '''

        :return: (path, self.delay)
        '''
        res = []
        for path in self.delay_paths:
            res.append((path, self.delay))

        return res

    def dump_delay_routes(self):
        '''

        :param service_id:
        :return: (path,  segment[0], segment[1]
        '''
        res = []
        for path, segments in self.delay_routes.items():
            for segment in segments:
                res.append((path, segment[0], segment[1]))

        return res

#this class build a full service topology
class ServiceTopoFull(AbstractServiceTopo):
    def __init__(self, sla, vhg_count, vcdn_count, hint_node_mappings=None):
        super(ServiceTopoFull, self).__init__(sla, vhg_count, vcdn_count, hint_node_mappings)

    def compute_service_topo(self, substrate, mapped_start_nodes, mapped_cdn_nodes, vhg_count, vcdn_count,
                                   hint_node_mappings=None):
        vhg_count = min(len(mapped_start_nodes), vhg_count)
        vcdn_count = min(vcdn_count, vhg_count)

        service = nx.DiGraph()
        service.add_node("S0", cpu=0)

        for i in range(1, vhg_count + 1):
            service.add_node("VHG%d" % i, type="VHG")

        for i in range(1, vcdn_count + 1):
            service.add_node("VCDN%d" % i, type="VCDN", cpu=0, delay=self.delay, ratio=0)

        for index, cdn in enumerate(mapped_cdn_nodes, start=1):
            service.add_node("CDN%d" % index, type="CDN", cpu=0, ratio=0)

        for key, slaNodeSpec in enumerate(mapped_start_nodes, start=1):
            service.add_node("S%d" % key, cpu=0, type="S", mapping=slaNodeSpec.topoNode.name, bandwidth=slaNodeSpec.attributes["bandwidth"])
            service.add_edge("S0", "S%d" % key, delay=sys.maxint, bandwidth=0)
            for i in range(1, vhg_count + 1):
                service.add_edge("S%d" % key, "VHG%d" % i)



        for vhg in range(1, vhg_count + 1):
            for vcdn in range(1, vcdn_count + 1):
                service.add_edge("VHG%d" % vhg, "VCDN%d" % vcdn)
            for index, cdn in enumerate(mapped_cdn_nodes, start=1):
                service.add_edge("VHG%d" % vhg, "CDN%d" % index)


        return service,[],{}


class ServiceTopoHeuristic(AbstractServiceTopo):
    def __init__(self, sla, vhg_count, vcdn_count, hint_node_mappings=None):
        super(ServiceTopoHeuristic, self).__init__(sla, vhg_count, vcdn_count, hint_node_mappings)

    def compute_service_topo(self, substrate, mapped_start_nodes, mapped_cdn_nodes, vhg_count, vcdn_count,hint_node_mappings=None):
        vhg_count = min(len(mapped_start_nodes), vhg_count)
        vcdn_count = min(vcdn_count, vhg_count)

        vmg_calc = get_vmg_calculator()
        vcdn_calc = get_vcdn_calculator()
        service = nx.DiGraph()
        service.add_node("S0", cpu=0)

        for i in range(1, vhg_count + 1):
            service.add_node("VHG%d" % i, type="VHG")

        for i in range(1, vcdn_count + 1):
            service.add_node("VCDN%d" % i, type="VCDN", cpu=105, delay=self.delay, ratio=0.35)

        for index, cdn in enumerate(mapped_cdn_nodes, start=1):
            service.add_node("CDN%d" % index, type="CDN", cpu=0, ratio=0.65)

        for key, slaNodeSpec in enumerate(mapped_start_nodes, start=1):
            service.add_node("S%d" % key, cpu=0, type="S", mapping=slaNodeSpec.topoNode.name)
            service.add_edge("S0", "S%d" % key, delay=sys.maxint, bandwidth=0)

        # create s<-> vhg edges
        for toponode_name, vmg_id in get_node_clusters(map(lambda x: x.topoNode.name, mapped_start_nodes), vhg_count,
                                                       substrate=substrate).items():
            s = [n[0] for n in service.nodes(data=True) if n[1].get("mapping", None) == toponode_name][0]
            service.add_edge(s, "VHG%d" % vmg_id, delay=sys.maxint, bandwidth=0)

        # create vhg <-> vcdn edges
        # here, each S "votes" for a vCDN and tell its VHG

        for toponode_name, vCDN_id in get_node_clusters(map(lambda x: x.topoNode.name, mapped_start_nodes), vcdn_count,
                                                        substrate=substrate).items():
            # get the S from the toponode_id
            s = [n[0] for n in service.nodes(data=True) if n[1].get("mapping", None) == toponode_name][0]

            vcdn = "VCDN%d" % vCDN_id
            # get the vhg from the S
            vhg = service[s].items()[0][0]

            # apply the votes
            if "votes" not in service.node[vhg]:
                service.node[vhg]["votes"] = collections.defaultdict(lambda: {})
                service.node[vhg]["votes"][vcdn] = 1
            else:
                service.node[vhg]["votes"][vcdn] = service.node[vhg]["votes"].get(vcdn, 0) + 1

        # create the edge according to the votes
        for vhg in [n[0] for n in service.nodes(data=True) if n[1].get("type") == "VHG"]:
            votes = service.node[vhg]["votes"]
            winners = max(votes.iteritems(), key=operator.itemgetter(1))
            if len(winners) == 1:
                service.add_edge(vhg, winners[0], bandwidth=0)
            else:
                service.add_edge(vhg, winners[0], bandwidth=0)

        # assign bandwidth
        workin_nodes = []
        for index, sla_node_spec in enumerate(mapped_start_nodes, start=1):
            service.node["S%d" % index]["bandwidth"] = sla_node_spec.attributes["bandwidth"]
            workin_nodes.append("S%d" % index)

        while len(workin_nodes) > 0:
            node = workin_nodes.pop()
            bandwidth = service.node[node].get("bandwidth", 0.0)
            children = service[node].items()
            for subnode, data in children:
                workin_nodes.append(subnode)
                edge_bw = bandwidth / float(len(children)) * service.node[subnode].get("ratio", 1.0)
                service[node][subnode]["bandwidth"] = edge_bw
                service.node[subnode]["bandwidth"] = service.node[subnode].get("bandwidth", 0.0) + edge_bw

        # assign CPU according to Bandwidth
        for vhg in self.get_nodes_by_type("VHG", service):
            service.node[vhg]["cpu"] = vmg_calc(service.node[vhg]["bandwidth"])

        # assign CPU according to Bandwidth
        #for vhg in self.get_nodes_by_type("VCDN", service):
        #    service.node[vhg]["cpu"] = vcdn_calc(service.node[vhg]["bandwidth"])

        # create delay path
        delay_path = {}
        delay_route = collections.defaultdict(lambda: [])
        for vcdn in self.get_nodes_by_type("VCDN", service):
            for s in self.get_nodes_by_type("S", service):
                try:
                    sp = shortest_path(service, s, vcdn)
                    key = "_".join(sp)
                    delay_path[key] = self.delay
                    for i in range(len(sp) - 1):
                        delay_route[key].append((sp[i], sp[i + 1]))

                except:
                    continue

        # add CDN edges if available
        try:
            if hint_node_mappings is not None:
                vhg_mapping = [(nmapping.node.name, nmapping.service_node.name) for nmapping in hint_node_mappings if
                               "VHG" in nmapping.service_node.name]
                cdn_mapping = [(nm.topoNode.name, "CDN%d" % index) for index, nm in
                               enumerate(mapped_cdn_nodes, start=1)]
                for vhg, cdn in get_vhg_cdn_mapping(vhg_mapping, cdn_mapping).items():
                    if vhg in service.node:
                        service.add_edge(vhg, cdn, bandwidth=service.node[vhg]["bandwidth"])

        except:
            traceback.print_exc()

        return service, delay_path, delay_route
