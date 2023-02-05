#!/usr/bin/env python3
'''
This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/>.
'''

from svgpathtools import Path, Line, QuadraticBezier, CubicBezier, Arc, svg2paths2, wsvg, parse_path, disvg
import svgpathtools
from numpy import array
import argparse
import json

#hacky workaround that doesn't require a __init__.py file in the submodule folder
import sys
sys.path.append("./fitCurves")
from fitCurves import fitCurve

MAX_ERROR = 1.00 #used when converting points to a bezier curve


# Takes a json string describing an array of Javascript Float32Array objects
# Returns an array of line segments; each segment is an array of point objects representing a continuous stroke
# Get the json string using JSON.stringify(tr) on https://www.calligrapher.ai/ after text is drawn
def json2points(jPoints):
    if not jPoints:
        jPoints = jTest
    segments = []
    segment = []
    
    points = json.loads(jPoints)
    #print(points)
    for point in points:
        p = {}
        x = point['0']
        y = point['1']
        end = point['2']
        p['x'] = x
        p['y'] = y
        #p['end'] = end
        segment.append(p)
        
        if end == 1:
            if len(segment) >= 2: #ignore single-point segments
                segments.append(segment)
            segment = []
    
    return segments

# Identifies the largest x and y offset that can be applied to remove excess whitespace, then applies that offset to every point
# Returns the new points (matches original segment groupings)
def moveToOrigin(segments):
    #print(segments[0])
    #max means the max we move everything without making any points go negative
    maxX = segments[0][0]['x']
    maxY = segments[0][0]['y']
    
    for segment in segments:
        for point in segment:
            maxX = point['x'] if point['x'] < maxX else maxX
            maxY = point['y'] if point['y'] < maxY else maxY
    
    newSegments = []
    for segment in segments:
        newSegment = []
        for point in segment:
            point['x'] = point['x'] - maxX
            point['y'] = point['y'] - maxY
            newSegment.append(point)
        newSegments.append(newSegment)
    
    #print(segments[0])
    return newSegments

#given a list of segments, find the distance between the highest and lowest points. Potentially useful for combining multiple lines without overlap
def getHeight(segments):
    minY = segments[0][0]['y']
    maxY = segments[0][0]['y']
    
    for segment in segments:
        for point in segment:
            minY = point['y'] if point['y'] < minY else minY
            maxY = point['y'] if point['y'] > maxY else maxY
    
    return maxY-minY

#take a list of points (grouped into line segments) and create a svgpathtools.Path object from them
def points2svg(segments):
    svgPath = Path()
    
    for segment in segments:
        if len(segment) < 2:
            continue
            
        prevPoint = segment[0]
        for point in segment[1:]:
            startX = prevPoint['x']
            startY = prevPoint['y']
            start = complex(startX, startY)
            
            endX = point['x']
            endY = point['y']
            end = complex(endX, endY)
            
            svgPath.append(Line(start, end))
            prevPoint = point
    
    return svgPath

#pass in an array of Complex points, e.g. from svgpathtools.Line.[start/end]
#returns an array of CubicBezier curves connecting all the points
def points2bezier(segment):
    #print(segment)
    #print(type(segment[0]))
    points = array([complex2pair(x) for x in segment]) #numpy.array()
    bSegment = []
    
    '''
    points = [] #array of floats; even index=X, odd index=Y
    for point in segment:
        x, y = complex2pair(point)
        points.append(x)
        points.append(y)
    '''
    #print(points)
    beziers = fitCurve(points, MAX_ERROR)
    #print(beziers)
    for b in beziers:
        s = pair2complex(b[0][0], b[0][1])
        c1 = pair2complex(b[1][0], b[1][1])
        c2 = pair2complex(b[2][0], b[2][1])
        e = pair2complex(b[3][0], b[3][1])
        bSegment.append(CubicBezier(s, c1, c2, e))
    
    return bSegment

#Convert a Complex object (used by svgpathtools) to a simple pair of floats
def complex2pair(c):
    return float(c.real), float(c.imag)
    
#convert a pair of floats into a Complex object
def pair2complex(x, y):
    #return Line(0+0j, "{}+{}j".format(x,y)).end
    return complex(x, y)


def main(args):
    svgPath = Path()
    
    with open(args.json) as jf:
        jPoints = jf.read()
        
        segments = json2points(jPoints)
        if not args.whitespace:
            segments = moveToOrigin(segments)
        
        if args.smooth:
            #AFAICT there are no programs to convert a SVG path to GCODE that uses G2/G3 (arc) commands; they all just do some sort of linear approximation. But smoothing them out seems to lead to smoother GCODE results at large sizes, so this is an option
            for segment in segments:
                #print(segment)
                cSegment = [ complex(p['x'], p['y']) for p in segment ]
                bSegment = points2bezier(cSegment)
                for b in bSegment:
                    svgPath.append(b)
        else:
            svgPath = points2svg(segments)
        
        
        wsvg(svgPath, filename=args.outfile, margin_size=0)
        return
        

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Convert points from the output of calligrapher.ai to 1-dimensional SVG paths for use with plotters and engravers. Go to the Console in your broswer's Developer Tools and run JSON.stringify(tr) to get the input JSON. You may need to un-escape double-quotes and remove the starting and ending quotes.")
    parser.add_argument("json", help="Path to a JSON file containing the array of points obtained from JSON.stringify(tr) on the calligrapher.ai website")
    parser.add_argument("--smooth", "-s", action="store_true", help="Smooth out paths using Cubic Bezier curve approximations. May cause small decrease in print speed when converted to linear GCODE.")
    parser.add_argument("--whitespace", "-w", action="store_true", help="Maintain any whitespace (offset from top-left corner) present in the original point array")
    parser.add_argument("--outfile", "-o", default="./output/output.svg", help="Path/filename to save output")
    
    args = parser.parse_args()
    main(args)




#this was intended to be used on the SVG output of the original handwriting-synthesis code repo, but I could never get that repo to compile.
def old_main(args):
    #path2 = parse_path("M 151,395 L407,485 L726.17662,160 L634,339")
    #print(path2)
    #exit()
    
    svg = "../handwriting-synthesis/img/banner - Copy.svg"
    paths, attributes, svg_attributes = svg2paths2(svg)
    newPath = Path()

    for i, path in enumerate(paths):
        segment = [] #array of objects of class 'complex' (representing xy point pairs)
        prevL = None
        for L in path:
            if not prevL:
                segment.append(L.start)
                #segment.append(L.end)
            elif L.start != prevL.end: #new segment
                #parse existing segment into bezier curves
                bSegment = points2bezier(segment)
                for b in bSegment:
                    newPath.append(b)
                
                segment = []
                segment.append(L.start)
            
            #continuation of current segment
            segment.append(L.end)
            prevL = L
        
        #print(segment)
        #print(type(segment[0]))
        points = array([complex2pair(x) for x in segment]) #numpy.array()
        
        '''
        points = [] #array of floats; even index=X, odd index=Y
        for point in segment:
            x, y = complex2pair(point)
            points.append(x)
            points.append(y)
        '''
        #print(points)
        beziers = fitCurve(points, MAX_ERROR)
        #print(beziers)
        for b in beziers:
            s = pair2complex(b[0][0], b[0][1])
            c1 = pair2complex(b[1][0], b[1][1])
            c2 = pair2complex(b[2][0], b[2][1])
            e = pair2complex(b[3][0], b[3][1])
            newPath.append(CubicBezier(s, c1, c2, e))
            
        
        #attribs = attributes[i]
        #print([{key: val} for key, val in attribs.items() if key != "d"])
    #print(newPath[0])
    #disvg(newPath)
    wsvg(newPath, filename='./test.svg')
